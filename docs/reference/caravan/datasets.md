# Caravan — available extensions

Every row pins a **specific Zenodo version record**, never the moving concept DOI, so a request is reproducible.
Sizes and checksums were read from the Zenodo REST API on 2026-08-01; re-check with
`earthlens datasets refresh caravan`.

All five extensions are **CC-BY-4.0**.

| `dataset=` | Catchments | Period | Record | Archive | Size | Range-readable |
|---|---|---|---|---|---|---|
| `grdc` | 5,356 | 1950–2023 | [15349031](https://doi.org/10.5281/zenodo.15349031) (v0.6) | `GRDC_Caravan_extension_csv.zip` | 8.84 GB | ✅ |
| `germany` | 1,887 | 1951–2020 | [14755229](https://doi.org/10.5281/zenodo.14755229) (v1.1.1) | `caravan_de.zip` | 6.09 GB | ✅ |
| `denmark` | 308 | 1981–2020 | [15200118](https://doi.org/10.5281/zenodo.15200118) | `Caravan_extension_DK.zip` | 0.52 GB | ✅ |
| `israel` | 95 | 1950–2024 | [15181680](https://doi.org/10.5281/zenodo.15181680) | `Caravan_extension_Israel_Ver4.zip` | 0.29 GB | ✅ |
| `base` | ≈16,300 | 1950–2023 | [15530022](https://doi.org/10.5281/zenodo.15530022) (v1.6) | `Caravan-csv.tar.gz` | 28.95 GB | ❌ opt-in |
| `base` (`version="1.2"`) | 6,830 | 1981–2020 | [7944025](https://doi.org/10.5281/zenodo.7944025) | `Caravan.zip` | 12.52 GB | ✅ |

!!! note "The `base` catchment count is arithmetic, not measured"
    A `.tar.gz` cannot be indexed without downloading it, so ≈16,300 comes from the Zenodo changelog: v0.4 reached
    6,830, v1.4 added 9,130 gauges previously excluded by the 100–2,000 km² area thresholds, and v1.6 moved
    CAMELS-AUS to v2 (+≈339). Every other row's count was measured from the archive index.

## `base` source datasets

`base` is one archive bundling seven source datasets. They are **not** separately downloadable and are **not**
valid `dataset=` values — request `base` and select their catchments by id or bbox.

| Source directory | Dataset | Catchments |
|---|---|---|
| `camels` | CAMELS-US | 482 |
| `camelsaus` | CAMELS-AUS v2 | 561 |
| `camelsbr` | CAMELS-BR | 376 |
| `camelscl` | CAMELS-CL | 314 |
| `camelsgb` | CAMELS-GB | 408 |
| `hysets` | HYSETS | 4,621 |
| `lamah` | LamaH-CE | 479 |

(Counts measured from the v1.2 index — the newest base release that can be read without downloading it.)

## Known but not wrapped

**Caravan MultiMet** ([14196771](https://doi.org/10.5281/zenodo.14196771) /
[14196772](https://doi.org/10.5281/zenodo.14196772)) adds nowcast and forecast weather products — CPC, IMERG v07,
CHIRPS, ECMWF IFS-HRES, GraphCast, CHIRPS-GEFS — for every Caravan basin. It ships **zarr cubes with lead-time
bands on a UTC-0 time base**, which is a gridded shape rather than the per-catchment daily table this backend
returns, so it needs its own backend and is deliberately out of scope here.

## Archive layout

Each archive holds the same inner structure, but **the directory it sits under differs per record and is absent
in two of them** — which is why member paths are resolved from the archive's own index rather than a template.

```
[<root>/]timeseries/csv/<source>/<gauge_id>.csv
[<root>/]timeseries/netcdf/<source>/<gauge_id>.nc
[<root>/]attributes/<source>/attributes_other_<source>.csv       # gauge lat/lon, country, area, name
[<root>/]attributes/<source>/attributes_caravan_<source>.csv     # climate indices
[<root>/]attributes/<source>/attributes_hydroatlas_<source>.csv
[<root>/]shapefiles/<source>/<source>_basin_shapes.shp           # + .shx .dbf .prj .cpg
[<root>/]licenses/<source>/license_<source>.md
```

| Extension | Root directory |
|---|---|
| `grdc` | `GRDC_Caravan_extension_csv/` (and `..._nc/` for the netCDF archive) |
| `israel` | `Caravan_extension_Israel_Ver4/` |
| `base` (1.2) | `Caravan/` |
| `denmark`, `germany` | *(none — members start at the archive root)* |
