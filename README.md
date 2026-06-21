# Landsat K-means: Хабаровск

Лабораторная работа по автоматической классификации мультиспектрального снимка Landsat методом K-means.

## Данные

- Сцена: `LC09_L2SP_113026_20240917_20240918_02_T1`
- Спутник: Landsat 9 OLI/TIRS
- Дата съемки: 2024-09-17
- Территория: Хабаровск и окрестности
- Path / Row: 113 / 026
- Облачность: 0.75%
- Система координат: WGS 84 / UTM Zone 53N (`EPSG:32653`)
- Использованные каналы: `SR_B2`, `SR_B3`, `SR_B4`, `SR_B5`, `SR_B6`, `SR_B7`

## Запуск

```bash
cd /Users/kirill/Documents/Homework/landsat-khabarovsk
.venv/bin/python src/landsat_kmeans_khabarovsk.py
```

## Результаты

- `report/Landsat_Khabarovsk_Kmeans_Report.docx` — итоговый отчет.
- `outputs/natural_color.png` — исходный снимок в натуральных цветах.
- `outputs/false_color.png` — ложноколорная композиция 5-4-3.
- `outputs/classification_map.png` — карта классификации K-means.
- `outputs/analysis_results.json` — метаданные, метрики и таблица площадей.
- `data/processed/khabarovsk_landsat_stack_B2_B7.tif` — подготовленный стек каналов.
- `data/processed/khabarovsk_kmeans_classified.tif` — классифицированный GeoTIFF.
