# JRC hazards — available datasets

The `earthlens.jrc` backend ships four curated datasets across three `kind`s.
The EFHM is selected by its facade key; the sea-level datasets are selected with
`product=`; the coastal summary is its own dataset, reached by the
`jrc:coastal-forecast` key or `dataset="sea_level_subseasonal_coastal"`.

| Dataset id | Facade key(s) | `kind` | Request axis | Native resolution | CRS | Output | Licence |
|---|---|---|---|---|---|---|---|
| `efhm` | `efhm`, `jrc-flood`, … | `flood_hazard_raster` | `return_periods` | ~90 m (3 arc-second) | EPSG:4326 | GeoTIFF | CC-BY-4.0 |
| `sea_level_medium_term` | `sea-level-forecast` (`product="medium_term"`) | `sea_level_gridded` | `reference_time` + bbox + `field` | 0.25° global | EPSG:4326 | multi-band GeoTIFF | CC-BY-4.0 |
| `sea_level_subseasonal` | `sea-level-forecast` (`product="subseasonal"`) | `sea_level_gridded` | `reference_time` + bbox + `field` | 0.25° global | EPSG:4326 | multi-band GeoTIFF | CC-BY-4.0 |
| `sea_level_subseasonal_coastal` | `jrc:coastal-forecast` | `sea_level_coastal` | `reference_time` (global) | per-country | — | `pandas.DataFrame` | CC-BY-4.0 |

- **EFHM return periods:** 10, 20, 30, 40, 50, 75, 100, 200, 500 years — given as
  an int (`100`), a string (`"100"`), or an `RP`-prefixed string (`"RP100"`);
  the default is `[100]`.
- **Sea-level cadence / horizon:** medium-term is issued twice daily with a
  15-day horizon (2022 → present); subseasonal is weekly with a ~46-day horizon
  (2026 → present). The server keeps only a **rolling window** of recent cycles,
  so older dates in those ranges are no longer retrievable. `reference_time="latest"` (the default) resolves the newest
  complete cycle from the JRC autoindex.
- **Sea-level fields:** each gridded cube exposes many derived 2-D fields
  (`TWL75`, `probability…`, `summary…`); a request crops one, defaulting to
  `TWL75` (the 75th-percentile total water level). Every forecast time step
  becomes a band of the written GeoTIFF.

## Listing the datasets programmatically

```python
from earthlens.jrc import Catalog

catalog = Catalog()
sorted(catalog.datasets)
# ['efhm', 'sea_level_medium_term', 'sea_level_subseasonal', 'sea_level_subseasonal_coastal']
catalog.get("efhm").return_periods
# [10, 20, 30, 40, 50, 75, 100, 200, 500]
catalog.get("sea_level_medium_term").default_field
# 'TWL75'
catalog.license_id
# 'CC-BY-4.0'
```

## Coverage & packaging

- **EFHM** — Europe and the Mediterranean Basin; one whole-Europe GeoTIFF per
  return period (`Europe_RP{n}_filled_depth.tif`) on the JRC CEMS-EFAS server.
  The backend reads only the AOI window over `/vsicurl`.
- **Sea-level** — global 0.25° cubes under
  `FLOODS/sea_level_forecasts/probabilistic_data_driven/{medium_term,subseasonal}_forecasts/`,
  laid out `YYYY/MM/DD/HH/` with a 0-byte `endFls` sentinel marking a complete
  cycle. The coastal CSV is global (keyed by ISO3 country code).

## Related coverage

- **Global** JRC river-flood hazard (`JRC/CEMS_GLOFAS/FloodHazard/v2_1`, ~90 m,
  also covering Europe) is available via the [`gee`](../gee/introduction.md)
  backend.
