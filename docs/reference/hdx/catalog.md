# Humanitarian Data Exchange (HDX) — catalog & tooling

The HDX backend ships a **curated** catalogue: a vetted set of
high-value datasets across seven humanitarian families, plus an
auto-generated `available_datasets:` index of the curated
organisations' wider holdings. A friendly dataset key (e.g.
`"kontur-population"`) resolves to an `HdxDataset` row carrying the CKAN
`hdx_id`, the owning `org`, the resource `formats` (CKAN labels), a
default `resource_filter`, and the informational `output_kinds`.

The catalogue lives as a directory of per-theme YAML files under
`src/earthlens/hdx/catalog/` (`population.yaml`, `boundaries.yaml`,
`buildings.yaml`, `socioeconomic.yaml`, `displacement.yaml`,
`food_security.yaml`, `conflict.yaml`) plus `_index.yaml` for the
auto-generated index. A dataset key declared in two files is rejected,
and unknown keys raise a `ValueError` with a did-you-mean hint.

## Curated datasets

| Key | HDX id | Org | Formats | Output kinds | Filter |
|-----|--------|-----|---------|--------------|--------|
| `acled-civilian-targeting` | `civilian-targeting-events-and-fatalities` | acled | XLSX | tabular | `(all)` |
| `acled-demonstrations` | `demonstration-events` | acled | XLSX | tabular | `(all)` |
| `acled-political-violence` | `political-violence-events-and-fatalities` | acled | XLSX | tabular | `(all)` |
| `cod-ab-cameroon` | `cod-ab-cmr` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-ghana` | `cod-ab-gha` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-kenya` | `cod-ab-ken` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-liberia` | `cod-ab-lbr` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-mali` | `cod-ab-mli` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-peru` | `cod-ab-per` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-senegal` | `cod-ab-sen` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `cod-ab-somalia` | `cod-ab-som` | ocha-fiss | SHP, GeoJSON, Geodatabase | vector | `SHP` |
| `hotosm-bangladesh-buildings` | `hotosm_bgd_buildings` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-bangladesh-roads` | `hotosm_bgd_roads` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-srilanka-buildings` | `hotosm_lka_buildings` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-srilanka-poi` | `hotosm_lka_points_of_interest` | hot | Geopackage, SHP, GeoJSON | vector | `Geopackage` |
| `hotosm-srilanka-roads` | `hotosm_lka_roads` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-srilanka-waterways` | `hotosm_lka_waterways` | hot | Geopackage, SHP, GeoJSON | vector | `Geopackage` |
| `hotosm-uganda-buildings` | `hotosm_uga_buildings` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-uganda-roads` | `hotosm_uga_roads` | hot | Geopackage, SHP | vector | `Geopackage` |
| `hotosm-uganda-waterways` | `hotosm_uga_waterways` | hot | Geopackage, SHP, GeoJSON | vector | `Geopackage` |
| `hotosm-uruguay-buildings` | `hotosm_ury_buildings` | hot | Geopackage, SHP | vector | `Geopackage` |
| `kontur-boundaries` | `kontur-boundaries` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `kontur-boundaries-afghanistan` | `kontur-boundaries-afghanistan` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `kontur-boundaries-albania` | `kontur-boundaries-albania` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `kontur-boundaries-algeria` | `kontur-boundaries-algeria` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `kontur-boundaries-angola` | `kontur-boundaries-angola` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `kontur-population` | `kontur-population-dataset` | kontur | Geopackage | vector | `*.gpkg.gz` |
| `meta-hrsl-drc` | `highresolutionpopulationdensitymaps-cod` | meta | zip | raster | `*.zip` |
| `meta-hrsl-global` | `highresolutionpopulationdensitymaps` | meta | GeoTIFF | raster | `GeoTIFF` |
| `meta-hrsl-nigeria` | `highresolutionpopulationdensitymaps-nga` | meta | zip | raster | `*.zip` |
| `meta-hrsl-south-africa` | `highresolutionpopulationdensitymaps-zaf` | meta | zip | raster | `*.zip` |
| `meta-hrsl-tanzania` | `highresolutionpopulationdensitymaps-tza` | meta | zip | raster | `*.zip` |
| `undp-gender-inequality-index` | `gender-inequality-index` | undp-human-development-reports-office | CSV | tabular | `CSV` |
| `undp-hdi-trends` | `human-development-index-trends` | undp-human-development-reports-office | XLSX | tabular | `(all)` |
| `undp-human-development-index` | `human-development-index-hdi` | undp-human-development-reports-office | CSV | tabular | `CSV` |
| `undp-multidimensional-poverty-index` | `multidimensional-poverty-index` | undp-human-development-reports-office | CSV | tabular | `CSV` |
| `undp-population-trends` | `population-trends` | undp-human-development-reports-office | CSV | tabular | `CSV` |
| `unhcr-refugee-monthly-arrivals` | `unhcr-refugee-monthly-arrivals-by-country` | unhcr | CSV, XLSX | tabular | `CSV` |
| `unhcr-refugees-from-colombia` | `number-of-refugees-from-colombia` | unhcr | CSV | tabular | `CSV` |
| `unhcr-refugees-from-yemen` | `number-of-refugees-from-yemen` | unhcr | CSV | tabular | `CSV` |
| `unhcr-statistical-yearbooks` | `unhcr-statistical-yearbooks` | unhcr | XLS | tabular | `(all)` |
| `wfp-coping-strategy-index` | `coping-strategy-index-csi` | wfp | CSV | tabular | `CSV` |
| `wfp-food-consumption-score` | `food-consumption-score-fcs` | wfp | CSV | tabular | `CSV` |
| `wfp-food-prices` | `wfp-food-prices` | wfp | CSV | tabular | `CSV` |
| `wfp-topline-figures` | `wfp-topline-figures` | wfp | CSV | tabular | `CSV` |
| `worldpop-egypt` | `worldpop-population-counts-for-egypt` | worldpop | GeoTIFF | raster | `GeoTIFF` |
| `worldpop-ethiopia` | `worldpop-population-counts-for-ethiopia` | worldpop | GeoTIFF | raster | `GeoTIFF` |
| `worldpop-kenya` | `worldpop-population-counts-for-kenya` | worldpop | GeoTIFF | raster | `GeoTIFF` |
| `worldpop-sudan` | `worldpop-population-counts-for-sudan` | worldpop | GeoTIFF | raster | `GeoTIFF` |

`(all)` means the dataset has no default filter, so every resource is
downloaded unless the request names a filter.

## The `available_datasets:` index

`catalog/_index.yaml` carries an informational list of HDX dataset ids
discovered for the curated organisations. It is **not** the full ~21k
catalogue and runtime code does not consume it — it is a browse aid and
the membership set the audit checks the curated keys against.

## Tooling — `tools/hdx/refresh_hdx_catalog.py`

A small `argparse` CLI over the read-only SDK:

```bash
# Rebuild the available_datasets index for the curated orgs (unioned
# with the curated ids, so every curated key is a member):
python tools/hdx/refresh_hdx_catalog.py refresh \
    --org kontur --org hot --org worldpop --org acled \
    --org unhcr --org wfp --org ocha-fiss --org meta \
    --include-curated

# Emit a ready-to-paste curated stanza for a new dataset:
python tools/hdx/refresh_hdx_catalog.py add-dataset my-key some-hdx-id

# Confirm every curated id still resolves live (drift check):
python tools/hdx/refresh_hdx_catalog.py audit --strict
```

- **`refresh`** runs `Dataset.search_in_hdx(...)` filtered by
  organisation / tag and rewrites `_index.yaml`.
- **`add-dataset`** reads one dataset live and prints a stanza,
  inferring `formats` and `output_kinds` from its resources.
- **`audit`** loads the bundled catalogue and checks that every curated
  `hdx_id` still resolves; `--strict` exits non-zero on any drift.
