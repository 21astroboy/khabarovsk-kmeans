from __future__ import annotations

import json
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import rasterio
from matplotlib.colors import ListedColormap
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import Window
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
OUTPUTS = ROOT / "outputs"
ASSETS = ROOT / "assets"

SCENE_ID = "LC09_L2SP_113026_20240917_20240918_02_T1"
KHABAROVSK_LON = 135.0719
KHABAROVSK_LAT = 48.4802
CLIP_SIZE_M = 120_000
N_CLUSTERS = 6
RANDOM_STATE = 42
TRAIN_SAMPLE = 250_000
SILHOUETTE_SAMPLE = 50_000

BANDS = {
    "B2": "Blue",
    "B3": "Green",
    "B4": "Red",
    "B5": "Near infrared",
    "B6": "SWIR 1",
    "B7": "SWIR 2",
}


def read_mtl(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"\s*([A-Z0-9_]+)\s=\s(.+)")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(2).strip().strip('"')
            values[match.group(1)] = value
    return values


def sr_to_reflectance(array: np.ndarray) -> np.ndarray:
    out = array.astype("float32")
    out = out * 0.0000275 - 0.2
    return np.clip(out, 0, 1)


def stretch_rgb(rgb: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    stretched = np.zeros_like(rgb, dtype="float32")
    for i in range(rgb.shape[0]):
        band = rgb[i]
        vals = band[valid_mask]
        low, high = np.nanpercentile(vals, [2, 98])
        stretched[i] = np.clip((band - low) / (high - low), 0, 1)
    return np.moveaxis(stretched, 0, -1)


def qa_valid_mask(qa: np.ndarray) -> np.ndarray:
    fill = (qa & (1 << 0)) != 0
    dilated_cloud = (qa & (1 << 1)) != 0
    cirrus = (qa & (1 << 2)) != 0
    cloud = (qa & (1 << 3)) != 0
    cloud_shadow = (qa & (1 << 4)) != 0
    snow = (qa & (1 << 5)) != 0
    return ~(fill | dilated_cloud | cirrus | cloud | cloud_shadow | snow)


def build_window() -> tuple[Window, dict, rasterio.Affine]:
    band_path = RAW / f"{SCENE_ID}_SR_B2.TIF"
    with rasterio.open(band_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = transformer.transform(KHABAROVSK_LON, KHABAROVSK_LAT)
        col, row = src.index(x, y)
        size_px = int(CLIP_SIZE_M / abs(src.transform.a))
        half = size_px // 2
        window = Window(
            col_off=max(0, col - half),
            row_off=max(0, row - half),
            width=size_px,
            height=size_px,
        ).round_offsets().round_lengths()
        transform = src.window_transform(window)
        profile = src.profile.copy()
        profile.update(
            height=int(window.height),
            width=int(window.width),
            transform=transform,
            compress="lzw",
            tiled=True,
        )
        return window, profile, transform


def read_clip(window: Window) -> tuple[np.ndarray, np.ndarray, dict]:
    arrays = []
    profile = None
    for band in BANDS:
        path = RAW / f"{SCENE_ID}_SR_{band}.TIF"
        with rasterio.open(path) as src:
            arr = src.read(1, window=window, resampling=Resampling.nearest)
            profile = src.profile.copy()
            arrays.append(sr_to_reflectance(arr))
    qa_path = RAW / f"{SCENE_ID}_QA_PIXEL.TIF"
    with rasterio.open(qa_path) as src:
        qa = src.read(1, window=window, resampling=Resampling.nearest)
    stack = np.stack(arrays, axis=0)
    valid = qa_valid_mask(qa) & np.all(stack > 0, axis=0)
    assert profile is not None
    return stack, valid, profile


def save_stack(stack: np.ndarray, valid: np.ndarray, profile: dict, transform) -> Path:
    out = PROCESSED / "khabarovsk_landsat_stack_B2_B7.tif"
    out_profile = profile.copy()
    out_profile.update(
        count=stack.shape[0],
        dtype="float32",
        nodata=-9999.0,
        height=stack.shape[1],
        width=stack.shape[2],
        transform=transform,
        compress="lzw",
        tiled=True,
    )
    data = stack.copy()
    data[:, ~valid] = -9999.0
    with rasterio.open(out, "w", **out_profile) as dst:
        dst.write(data)
        for idx, band_name in enumerate(BANDS, start=1):
            dst.set_band_description(idx, f"{band_name} {BANDS[band_name]}")
    return out


def classify(stack: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, dict]:
    pixels = stack.reshape(stack.shape[0], -1).T
    valid_flat = valid.reshape(-1)
    X = pixels[valid_flat]
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    rng = np.random.default_rng(RANDOM_STATE)
    train_idx = rng.choice(X_scaled.shape[0], min(TRAIN_SAMPLE, X_scaled.shape[0]), replace=False)

    metrics = []
    for k in range(5, 9):
        km = KMeans(n_clusters=k, init="k-means++", n_init=10, max_iter=300, random_state=RANDOM_STATE)
        sample_labels = km.fit_predict(X_scaled[train_idx])
        sil_idx = rng.choice(len(train_idx), min(SILHOUETTE_SAMPLE, len(train_idx)), replace=False)
        sil = silhouette_score(X_scaled[train_idx][sil_idx], sample_labels[sil_idx])
        metrics.append({"k": k, "wcss": float(km.inertia_), "silhouette": float(sil)})

    model = KMeans(n_clusters=N_CLUSTERS, init="k-means++", n_init=10, max_iter=300, random_state=RANDOM_STATE)
    model.fit(X_scaled[train_idx])
    labels = model.predict(X_scaled)

    classified = np.full(valid_flat.shape, -1, dtype="int16")
    classified[valid_flat] = labels
    classified = classified.reshape(valid.shape)

    centers_scaled = model.cluster_centers_
    centers = scaler.inverse_transform(centers_scaled)
    counts = np.bincount(labels, minlength=N_CLUSTERS)
    meta = {"metrics": metrics, "centers": centers.tolist(), "counts": counts.tolist()}
    return classified, meta


def save_classified(classified: np.ndarray, profile: dict, transform) -> Path:
    out = PROCESSED / "khabarovsk_kmeans_classified.tif"
    out_profile = profile.copy()
    out_profile.update(
        count=1,
        dtype="int16",
        nodata=-1,
        height=classified.shape[0],
        width=classified.shape[1],
        transform=transform,
        compress="lzw",
        tiled=True,
    )
    with rasterio.open(out, "w", **out_profile) as dst:
        dst.write(classified, 1)
    return out


def interpret_clusters(meta: dict) -> dict[int, dict[str, str]]:
    centers = np.asarray(meta["centers"], dtype="float32")
    b2, b3, b4, b5, b6, b7 = centers.T
    ndvi = (b5 - b4) / (b5 + b4 + 1e-6)
    mndwi = (b3 - b6) / (b3 + b6 + 1e-6)
    ndbi = (b6 - b5) / (b6 + b5 + 1e-6)

    remaining = set(range(N_CLUSTERS))
    mapping: dict[int, dict[str, str]] = {}

    water = int(np.argmax(mndwi))
    mapping[water] = {"name": "Водные объекты", "color": "#2166AC"}
    remaining.remove(water)

    dense_forest = int(max(remaining, key=lambda i: ndvi[i]))
    mapping[dense_forest] = {"name": "Плотная древесная растительность", "color": "#1B7837"}
    remaining.remove(dense_forest)

    sparse_veg = int(max(remaining, key=lambda i: ndvi[i]))
    mapping[sparse_veg] = {"name": "Разреженная растительность и сельхозугодья", "color": "#7FBF3F"}
    remaining.remove(sparse_veg)

    built = int(max(remaining, key=lambda i: ndbi[i] + b4[i]))
    mapping[built] = {"name": "Городская застройка", "color": "#F4A3B5"}
    remaining.remove(built)

    industrial = int(max(remaining, key=lambda i: b6[i] + b7[i]))
    mapping[industrial] = {"name": "Промышленные зоны, дороги и открытый грунт", "color": "#F28E2B"}
    remaining.remove(industrial)

    for i in remaining:
        mapping[i] = {"name": "Смешанные открытые территории", "color": "#B59F3B"}

    for i in range(N_CLUSTERS):
        mapping[i]["ndvi"] = f"{ndvi[i]:.3f}"
        mapping[i]["mndwi"] = f"{mndwi[i]:.3f}"
        mapping[i]["ndbi"] = f"{ndbi[i]:.3f}"
    return mapping


def image_extent(transform, width: int, height: int) -> tuple[float, float, float, float]:
    left = transform.c
    top = transform.f
    right = left + width * transform.a
    bottom = top + height * transform.e
    return left, right, bottom, top


def format_lon(value: float) -> str:
    hemi = "E" if value >= 0 else "W"
    return f"{abs(value):.2f}°{hemi}"


def format_lat(value: float) -> str:
    hemi = "N" if value >= 0 else "S"
    return f"{abs(value):.2f}°{hemi}"


def add_geo_grid(ax, extent, crs) -> None:
    left, right, bottom, top = extent
    to_geo = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xticks = np.linspace(left, right, 5)
    yticks = np.linspace(bottom, top, 5)
    mid_y = (bottom + top) / 2
    mid_x = (left + right) / 2
    xlabels = [format_lon(to_geo.transform(x, mid_y)[0]) for x in xticks]
    ylabels = [format_lat(to_geo.transform(mid_x, y)[1]) for y in yticks]
    ax.set_xticks(xticks)
    ax.set_yticks(yticks)
    ax.set_xticklabels(xlabels, fontsize=8)
    ax.set_yticklabels(ylabels, fontsize=8)
    ax.grid(color="#1F4D78", alpha=0.35, linewidth=0.7, linestyle="--")
    ax.tick_params(length=0)


def add_scale_bar(ax, extent, length_km: int = 20) -> None:
    left, right, bottom, top = extent
    width = right - left
    height = top - bottom
    length_m = length_km * 1000
    x0 = left + width * 0.08
    y0 = bottom + height * 0.08
    ax.plot([x0, x0 + length_m], [y0, y0], color="black", lw=4, solid_capstyle="butt")
    ax.plot([x0, x0], [y0 - height * 0.012, y0 + height * 0.012], color="black", lw=2)
    ax.plot([x0 + length_m, x0 + length_m], [y0 - height * 0.012, y0 + height * 0.012], color="black", lw=2)
    ax.text(x0 + length_m / 2, y0 - height * 0.025, f"{length_km} км", ha="center", va="top", fontsize=10)


def add_north_arrow(ax, extent) -> None:
    left, right, bottom, top = extent
    width = right - left
    height = top - bottom
    x = left + width * 0.08
    y = bottom + height * 0.20
    ax.annotate(
        "N",
        xy=(x, y + height * 0.08),
        xytext=(x, y),
        arrowprops=dict(facecolor="black", width=8, headwidth=22),
        ha="center",
        va="center",
        fontsize=18,
        fontweight="bold",
    )


def plot_outputs(stack: np.ndarray, valid: np.ndarray, classified: np.ndarray, profile: dict, transform, mapping: dict) -> None:
    natural = stretch_rgb(stack[[2, 1, 0]], valid)
    false_color = stretch_rgb(stack[[3, 2, 1]], valid)
    natural[~valid] = 1
    false_color[~valid] = 1
    extent = image_extent(transform, stack.shape[2], stack.shape[1])

    for name, img, title in [
        ("natural_color.png", natural, "Landsat-9 Natural Color 4-3-2: Хабаровск"),
        ("false_color.png", false_color, "Landsat-9 False Color 5-4-3: Хабаровск"),
    ]:
        fig, ax = plt.subplots(figsize=(9, 9), dpi=180)
        ax.imshow(img, extent=extent)
        ax.set_title(title, fontsize=13)
        add_geo_grid(ax, extent, profile["crs"])
        add_scale_bar(ax, extent)
        add_north_arrow(ax, extent)
        fig.tight_layout()
        fig.savefig(OUTPUTS / name, bbox_inches="tight")
        plt.close(fig)

    colors = [mapping[i]["color"] for i in range(N_CLUSTERS)]
    names = [mapping[i]["name"] for i in range(N_CLUSTERS)]
    display = classified.astype("float32")
    display[display < 0] = np.nan

    fig, ax = plt.subplots(figsize=(9, 11), dpi=180)
    ax.imshow(display, cmap=ListedColormap(colors), vmin=0, vmax=N_CLUSTERS - 1, extent=extent)
    ax.set_title("Автоматическая классификация K-means (k=6): Хабаровск и окрестности", fontsize=12)
    add_geo_grid(ax, extent, profile["crs"])
    add_scale_bar(ax, extent)
    add_north_arrow(ax, extent)
    handles = [mpatches.Patch(color=colors[i], label=names[i]) for i in range(N_CLUSTERS)]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.11), ncol=1, fontsize=9, frameon=False)
    fig.subplots_adjust(bottom=0.24, top=0.94, left=0.08, right=0.98)
    fig.savefig(OUTPUTS / "classification_map.png", bbox_inches="tight")
    plt.close(fig)


def area_table(classified: np.ndarray, mapping: dict) -> list[dict[str, object]]:
    pixel_area_km2 = 30 * 30 / 1_000_000
    valid = classified >= 0
    total = int(valid.sum())
    rows = []
    for cluster in range(N_CLUSTERS):
        count = int((classified == cluster).sum())
        rows.append(
            {
                "cluster": cluster,
                "color": mapping[cluster]["color"],
                "name": mapping[cluster]["name"],
                "pixels": count,
                "area_km2": round(count * pixel_area_km2, 2),
                "share_percent": round(count / total * 100, 2),
                "ndvi": mapping[cluster]["ndvi"],
                "mndwi": mapping[cluster]["mndwi"],
                "ndbi": mapping[cluster]["ndbi"],
            }
        )
    rows.sort(key=lambda r: r["cluster"])
    return rows


def main() -> None:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)

    mtl = read_mtl(RAW / f"{SCENE_ID}_MTL.txt")
    window, profile, transform = build_window()
    stack, valid, src_profile = read_clip(window)
    profile.update(src_profile)
    profile.update(height=stack.shape[1], width=stack.shape[2], transform=transform)
    stack_path = save_stack(stack, valid, profile, transform)
    classified, meta = classify(stack, valid)
    classified_path = save_classified(classified, profile, transform)
    mapping = interpret_clusters(meta)
    rows = area_table(classified, mapping)
    plot_outputs(stack, valid, classified, profile, transform, mapping)

    result = {
        "scene_id": SCENE_ID,
        "stack_path": str(stack_path),
        "classified_path": str(classified_path),
        "metadata": {
            "spacecraft": mtl.get("SPACECRAFT_ID"),
            "sensor": mtl.get("SENSOR_ID"),
            "date_acquired": mtl.get("DATE_ACQUIRED"),
            "path": mtl.get("WRS_PATH"),
            "row": mtl.get("WRS_ROW"),
            "cloud_cover": mtl.get("CLOUD_COVER"),
            "processing_level": mtl.get("PROCESSING_LEVEL"),
            "product_id": mtl.get("LANDSAT_PRODUCT_ID"),
            "utm_zone": mtl.get("UTM_ZONE"),
            "datum": mtl.get("DATUM"),
            "crs": str(profile.get("crs")),
            "clip_size_m": CLIP_SIZE_M,
        },
        "kmeans": {
            "n_clusters": N_CLUSTERS,
            "random_state": RANDOM_STATE,
            "train_sample": TRAIN_SAMPLE,
            "metrics": meta["metrics"],
        },
        "area_table": rows,
    }
    (OUTPUTS / "analysis_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
