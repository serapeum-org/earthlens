# GHSL — Available datasets

The catalog (`earthlens.ghsl.Catalog`, backed by `ghsl_data_catalog.yaml`)
curates **18 GLOBE products** across releases R2023A, R2022A (GHS-LAND), and
R2025A (GHS-WUP projections). Inspect it programmatically:

```python
from earthlens.ghsl import Catalog
cat = Catalog()
cat.available_products()           # all 18 canonical codes
cat.resolve("population")          # -> "GHS_POP"
cat.get("GHS_SMOD").legend         # categorical class-code -> label map
cat.validate("GHS_POP", "R2023A", 2020, "100m")   # raises on an invalid combo
```

## Availability matrix

A resolution implies its **source CRS** (metric → Mollweide ESRI:54009;
arc-second → WGS84 EPSG:4326). Aliases are case-insensitive.

| Code | Aliases | Release | Epochs | Resolutions | Kind |
|---|---|---|---|---|---|
| `GHS_POP` | population, pop | R2023A | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | raster |
| `GHS_BUILT_S` | built_surface | R2023A | 1975–2030 / 5; **2018** | 100 m, 1 km, 3″, 30″; **10 m** (2018) | raster |
| `GHS_BUILT_S_NRES` | built_surface_nres | R2023A | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | raster |
| `GHS_BUILT_V` | built_volume | R2023A | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | raster |
| `GHS_BUILT_V_NRES` | built_volume_nres | R2023A | 1975–2030 / 5 | 100 m, 1 km, 3″, 30″ | raster |
| `GHS_BUILT_H_ANBH` | built_height, anbh | R2023A | 2018 | 100 m, 3″ | raster |
| `GHS_BUILT_H_AGBH` | gross_building_height, agbh | R2023A | 2018 | 100 m, 3″ | raster |
| `GHS_BUILT_C_MSZ` | built_class, built_c_msz | R2023A | 2018 | 10 m | raster (categorical) |
| `GHS_BUILT_C_FUN` | built_function, built_c_fun | R2023A | 2018 | 10 m | raster (categorical) |
| `GHS_SMOD` | settlement_model, smod, degurba | R2023A | 1975–2030 / 5 | 1 km, 30″ | raster (categorical) |
| `GHS_LAND` | land, land_fraction | **R2022A** | 2018 | 10 m, 100 m, 1 km | raster |
| `GHS_DUC` | duc | R2023A | 1975–2030 / 5 | — | tabular |
| `GHS_WUP_POP` | wup_population | R2025A | 1975–2100 / 5 | 1 km, 30″ | raster |
| `GHS_WUP_BUILT_S` | wup_built_surface | R2025A | 1975–2100 / 5 | 1 km, 30″ | raster |
| `GHS_WUP_DEGURBA` | wup_degurba | R2025A | 1975–2100 / 5 | 1 km | raster (categorical) |
| `GHS_WUP_DUC` | wup_duc | R2025A | 2025–2100 | — | tabular |
| `GHS_WUP_MTUC` | wup_mtuc | R2025A | 2025–2100 | — | tabular |
| `GHS_WUP_COUNTRY_STATS` | wup_country_stats | R2025A | 2025–2100 | — | tabular |

Sub-products (`_NRES`, `_ANBH`/`_AGBH`, `_MSZ`/`_FUN`) live under their parent
**family** directory on the JRC tree but carry their own file-stem token; the
catalog's `family` field captures that.

## Categorical legends

Categorical products carry a `legend` (class code → label) and `colors`, and
write a `{file}.legend.json` sidecar next to the GeoTIFF.

### `GHS_SMOD` / `GHS_WUP_DEGURBA` — Degree of Urbanisation (settlement model)

| Code | Class |
|---|---|
| 30 | Urban Centre |
| 23 | Dense Urban Cluster |
| 22 | Semi-dense Urban Cluster |
| 21 | Suburban / Peri-urban |
| 13 | Rural Cluster |
| 12 | Low Density Rural |
| 11 | Very Low Density Rural |
| 10 | Water |

### `GHS_BUILT_C_MSZ` — Morphological Settlement Zone

| Code | Class | | Code | Class |
|---|---|---|---|---|
| 1 | Open space, low vegetation | | 13 | Residential, 6–15 m |
| 2 | Open space, medium vegetation | | 14 | Residential, 15–30 m |
| 3 | Open space, high vegetation | | 15 | Residential, > 30 m |
| 4 | Open space, water | | 21 | Non-residential, ≤ 3 m |
| 5 | Open space, road surface | | 22 | Non-residential, 3–6 m |
| 11 | Residential, ≤ 3 m | | 23 | Non-residential, 6–15 m |
| 12 | Residential, 3–6 m | | 24 | Non-residential, 15–30 m |
| | | | 25 | Non-residential, > 30 m |

### `GHS_BUILT_C_FUN` — Functional classification

| Code | Class |
|---|---|
| 1 | Residential |
| 2 | Non-residential |

## Catalog tooling

`tools/ghsl/refresh_ghsl_catalog.py` maintains this catalog against the live
tree:

```bash
# HEAD a representative artefact per curated product + check legends:
python tools/ghsl/refresh_ghsl_catalog.py validate --strict
# list the (epoch, resolution) combos a family dir actually offers:
python tools/ghsl/refresh_ghsl_catalog.py probe GHS_POP_GLOBE_R2023A
# regenerate the bundled Mollweide tile schema:
python tools/ghsl/refresh_ghsl_catalog.py refresh-tiles
```
