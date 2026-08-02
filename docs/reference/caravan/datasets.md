# Caravan — available extensions

Every row pins a **specific Zenodo version record**, never the moving concept DOI, so a request is reproducible.
Sizes and checksums were read from the Zenodo REST API on 2026-08-01; re-check with
`earthlens datasets refresh caravan`.

All seven extensions are **CC-BY-4.0**.

| `dataset=` | Catchments | Period | Record | Archive | Size | Range-readable |
|---|---|---|---|---|---|---|
| `grdc` | 5,356 | 1950–2023 | [15349031](https://doi.org/10.5281/zenodo.15349031) (v0.6) | `GRDC_Caravan_extension_csv.zip` | 8.84 GB | ✅ |
| `germany` | 1,887 | 1951–2020 | [14755229](https://doi.org/10.5281/zenodo.14755229) (v1.1.1) | `caravan_de.zip` | 6.09 GB | ✅ |
| `spain` | 269 | 1990–2020 | [15040948](https://doi.org/10.5281/zenodo.15040948) (v1.1.0) | `CAMELS-ES_v110.zip` | 0.40 GB | ✅ |
| `czechia` | 249 | 1950–**2025** | [17769325](https://doi.org/10.5281/zenodo.17769325) | `Caravan-Extension-CZ.zip` | 0.91 GB | ✅ |
| `denmark` | 308 | 1981–2020 | [15200118](https://doi.org/10.5281/zenodo.15200118) | `Caravan_extension_DK.zip` | 0.52 GB | ✅ |
| `israel` | 95 | 1950–2024 | [15181680](https://doi.org/10.5281/zenodo.15181680) | `Caravan_extension_Israel_Ver4.zip` | 0.29 GB | ✅ |
| `base` | 16,299 | 1950–2023 | [15530022](https://doi.org/10.5281/zenodo.15530022) (v1.6) | `Caravan-csv.tar.gz` | 28.95 GB | ❌ opt-in |
| `base` (`version="1.2"`) | 6,830 | 1981–2020 | [7944025](https://doi.org/10.5281/zenodo.7944025) | `Caravan.zip` | 12.52 GB | ✅ |

!!! note "Every count here is measured, `base` included"
    `base` is the expensive one: a `.tar.gz` cannot be indexed without downloading it, so its 16,299 was
    established by pulling the full 28.95 GB archive and indexing it (2026-08-02) — `hysets` 12,162,
    `camelsbr` 870, `lamah` 859, `camels` 671, `camelsgb` 671, `camelsaus` 561, `camelscl` 505. The Zenodo
    changelog implies ≈16,300 (6,830 at v0.4, +9,130 at v1.4, +CAMELS-AUS v2 at v1.6); the measurement lands
    one below that.

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

!!! note "Two extensions are published under their source dataset's name"
    `czechia` and `spain` are titled *CAMELS-CZ* and *CAMELS-ES* — the word "Caravan" never appears in either
    title, only inside the record. A title-based search misses them entirely, which is why
    `earthlens datasets refresh caravan` searches record **content**. `czechia` also carries the most recent
    series of any extension, running to mid-2025.

## Known but not wrapped

Every Caravan-related record the project announces or that a content search surfaces has been checked. These are
the ones deliberately **not** wrapped, each with the reason:

| Record | Why not |
|---|---|
| **Caravan MultiMet** ([14196771](https://doi.org/10.5281/zenodo.14196771) / [14196772](https://doi.org/10.5281/zenodo.14196772)) | zarr cubes with lead-time bands on a UTC-0 base — a gridded shape, not a per-catchment table |
| **Caravan-Qual (lite)** ([21337532](https://doi.org/10.5281/zenodo.21337532)) | CC-BY-4.0, but per-*parameter* water-quality tables (`wqms-csv/pH.csv`, …) plus zarr — verified to expose no `timeseries/` members |
| **MSWEP4Caravan** ([19199860](https://doi.org/10.5281/zenodo.19199860)) | Caravan-shaped, but **CC-BY-NC-4.0** — non-commercial sources stay out of the default path |
| **Caravan extension Iceland** (`lamahice`) | published on **HydroShare**, not Zenodo, and CC-BY-NC-4.0 |
| **Caravan-AUS-VIC** ([18736844](https://doi.org/10.5281/zenodo.18736844)) | **deleted by its author** on 2026-03-01; the record returns HTTP 410 |
| **GAGES II** | announced in the project's thread #24 but never published with a DOI |
| **CAMELS-CH** ([15025258](https://doi.org/10.5281/zenodo.15025258)) | native CAMELS layout, not Caravan's — verified by opening it |

`earthlens datasets refresh caravan` re-checks this list on every run, so a record that later gains a Caravan
archive, or a new extension entirely, shows up as `discovered` rather than staying invisible.

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
| `czechia` | `Caravan-Extension-CZ/` |
| `spain` | `v110/` — a bare version directory |
| `base` (1.2) | `Caravan/` |
| `denmark`, `germany` | *(none — members start at the archive root)* |

Six shapes across seven extensions, no two derivable from the extension name. `czechia` also uses a **singular**
`license/` directory where every other archive ships `licenses/`. This is why member paths are resolved from the
archive's own index rather than composed from a template.
