# GHSL — Available datasets

The catalog (`earthlens.ghsl.Catalog`, backed by the per-family `catalog/`
directory — `population.yaml`, `built-up.yaml`, `settlement.yaml`, `land.yaml`,
`projections.yaml`, `legacy-statistical.yaml`, plus `_index.yaml`) curates
**29 products** across releases R2019A / R2022A / R2023A / R2024A / R2025A and
the GLOBE / EUROPE / ARCTIC regions. Inspect it programmatically:

```python
from earthlens.ghsl import Catalog
cat = Catalog()
cat.available_products()           # all 29 canonical codes
cat.resolve("population")          # -> "GHS_POP"
cat.get("GHS_SMOD").legend         # categorical class-code -> label map
cat.validate("GHS_POP", "R2023A", 2020, "100m")   # raises on an invalid combo
cat.validate("GHS_POP", "R2022A", 2020, "100m")   # legacy release also works
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

## Legacy & regional families

Beyond the headline R2023A/R2025A surface, the catalog also curates older
releases and regional / statistical products:

- **Legacy R2022A GLOBE raster** of the current products — `GHS_POP`,
  `GHS_SMOD`, `GHS_BUILT_S`(+`_NRES`), `GHS_BUILT_V`(+`_NRES`),
  `GHS_BUILT_H_ANBH`/`_AGBH`, `GHS_BUILT_C_MSZ`/`_FUN`, plus the R2022A-only
  `GHS_BUILT_C_VEG`. R2022A stops at epoch 2020, is **Mollweide-only** (the
  WGS84 arc-second variants arrived in R2023A), and nests its per-epoch
  directories under a sub-product directory (handled transparently). Request
  with `release="R2022A"` (these products also exist at R2023A, so the release
  must be given explicitly).
- **Tabular / statistical families** (downloaded as a versioned `.zip` table,
  no raster pipeline): `GHS_AGE` (R2025A), `GHS_COUNTRY_STATS_MT` (R2024A),
  `GHS_FUA_UCDB2015` / `GHS_STAT_DUCMT` / `GHS_STAT_UCDB2015MT` (R2019A),
  the EUROPE LAU products `GHS_BUILT_LAUSTAT` (R2023A) / `GHS_BUILT_LAU2STAT`
  (R2022A), and the ARCTIC regional tables `GHS_BUSS` / `GHS_POP_ARCTIC` /
  `GHS_SMOD_ARCTIC` (R2025A). Each exists at a single release, so `release=`
  is auto-detected when omitted (a request may even mix products from
  different releases); the table lands under `path/{code}/`.

### Deliberately not curated

The remaining JRC families are excluded **by nature**, not oversight (the
`tools/ghsl/refresh_ghsl_catalog.py` manifest records them):

- **JRC-marked obsolete** (each carries a `---OBSOLETE_RELEASE---` marker and
  dead CRS/resolution tokens): `GHS_BUILT_LDSMT*`, `GHS_POP_GPW4`,
  `GHS_POP_EUROSTAT`, `GHS_BUILT_S1NODSM`, `GHS_BUILT_S2comp2018`,
  `GHS_SMOD_POP*`, and the empty R2022A `GHS_DUC`.
- **Imagery / vector products** that need a different backend, not a raster
  catalog row: `GHS_SDGSAT1` (RGB imagery), `GHS_UCDB` / `GHS_OBAT` (vector
  CSV / GeoPackage), `GHS_composite_S2` (Sentinel-2 UTM composite),
  `GHS_SDATA` (derived multi-layer).
- **ARCTIC raster** (`GHS_BUILT_S/V/H_ARCTIC`) — a polar version-directory
  layout on a non-Mollweide grid, not the epoch-tiled scheme this backend
  models. The ARCTIC *tabular* products above are curated; the ARCTIC rasters
  are not.
- **`GHS_POP_MT` (R2019A)** — bespoke 250 m / 9-arc-second resolution tokens
  outside the standard GHSL set.

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
