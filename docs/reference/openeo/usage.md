# openEO backend — usage

## Request shape

```python
EarthLens(
    data_source="openeo",
    variables={collection_or_recipe_key: [band, ...]},
    start="YYYY-MM-DD", end="YYYY-MM-DD",
    lat_lim=[lat_min, lat_max],
    lon_lim=[lon_min, lon_max],
    path="out",
    # optional openEO kwargs:
    endpoint=None,            # "cdse" (default) / "cdse-federation" / "openeo-platform" / URL
    process=None,             # explicit recipe key, overriding the one inferred from the request key
    execute="sync",           # "sync" (default) or "batch"
    output_format="GTiff",    # "GTiff" (default) or "netCDF"
    max_cloud_cover=None,     # server-side property filter on load_collection (Sentinel-2)
    # OIDC credential kwargs (see Authentication): client_id / client_secret / refresh_token / provider_id
)
```

`variables` maps each key to a band list. A key is **either**:

* a **collection** (e.g. `"sentinel-2-l2a"`) → the default graph
  `load_collection → clip → save`, loading the bands you list (or the
  collection's `default_bands` when the list is empty); or
* a **recipe** (e.g. `"sentinel-2-l2a-ndvi-monthly"`) → the recipe's fixed
  graph. An empty band list uses the recipe's own bands; a non-empty list
  overrides them.

Multiple keys per call are allowed — one output file is written per key.

### Kwargs

| Kwarg | Default | Meaning |
|---|---|---|
| `endpoint` | `"cdse"` | which openEO backend to target ([Introduction](introduction.md)) |
| `process` | `None` | apply this recipe to **every** request key, overriding the inferred recipe/collection |
| `execute` | `"sync"` | `"sync"` = `DataCube.download` (size-capped); `"batch"` = a polled batch job |
| `output_format` | `"GTiff"` | openEO output format; a recipe's own `output_format` wins when set |
| `max_cloud_cover` | `None` | forwarded to `load_collection(max_cloud_cover=…)`; only for optical collections that expose `eo:cloud_cover` (Sentinel-2). Passing it to a non-optical collection (SAR, atmosphere, DEM) raises `ValueError`. |

> `output_format` is named distinctly from `fmt` (the date-string format
> inherited from the facade) on purpose — the two never collide.

## Server-side aggregation (`aggregate=`)

Because openEO reduces **server-side**, an `aggregate=AggregationConfig(...)`
becomes a native `aggregate_temporal_period` node in the graph (before
`save_result`) — the only backend where `aggregate=` costs **no** client compute
and no pyramids reducer.

```python
from earthlens.aggregate import AggregationConfig

EarthLens(
    data_source="openeo",
    variables={"sentinel-2-l2a": ["B04", "B08"]},
    start="2023-01-01", end="2023-12-31",
    lat_lim=[40.40, 40.45], lon_lim=[3.67, 3.72],
    path="out",
).download(aggregate=AggregationConfig(freq="1MS", op="mean"))
```

The aggregator's pandas `freq` is mapped to an openEO **calendar period**, and
its `op` to an openEO **reducer**:

| `AggregationConfig.freq` | openEO `period` | | `AggregationConfig.op` | openEO `reducer` |
|---|---|---|---|---|
| `D` / `7D` | `day` | | `auto` | `mean` |
| `W` | `week` | | `mean` | `mean` |
| `10D` | `dekad` (10-day) | | `sum` | `sum` |
| `MS` / `M` | `month` | | `min` / `max` | `min` / `max` |
| `QS` / `Q` | `season` | | `std` | `sd` |
| `YS` / `Y` | `year` | | | |

A `freq` with no calendar equivalent raises `NotImplementedError` naming the
supported periods.

## Execution: sync vs batch

* **`execute="sync"`** (default) — `DataCube.download(path, format=…)`. Fast, but
  the openEO sync endpoint has **size/time caps**; use it for small AOIs and
  short windows.
* **`execute="batch"`** — `cube.create_job().start_and_wait().get_results()
  .download_file(path)`. The backend submits a batch job and **polls to
  completion**, like GEE's async export. Use it for large AOIs / long windows.

```python
EarthLens(
    data_source="openeo",
    variables={"sentinel-2-l2a-cloud-masked-composite": []},
    start="2023-06-01", end="2023-08-31",
    lat_lim=[40.0, 41.0], lon_lim=[3.0, 4.0],
    path="out",
    execute="batch",
).download()
```

## Return value

`download()` returns a `list[pathlib.Path]` — one written file per request key.
The suffix follows the format (`.tif` for `GTiff`, `.nc` for `netCDF`); a
recipe's `output_format` overrides the backend default.

## Catalog tooling

Three scripts under `tools/openeo/` (the same `refresh` / `probe` / `audit` set
the other backends ship). Listing and describing are **anonymous** — no OIDC
login is needed for any of them.

**`refresh_openeo_catalog.py`** — keep the informational index current and
validate a recipe:

```bash
# Rebuild available_collections / available_processes in _index.yaml:
python tools/openeo/refresh_openeo_catalog.py refresh

# Print the regenerated index without writing it:
python tools/openeo/refresh_openeo_catalog.py refresh --dry-run

# Check a recipe's base collection + process names exist on the backend:
python tools/openeo/refresh_openeo_catalog.py validate-recipe sentinel-2-l2a-ndvi-monthly
```

**`audit_openeo_datasets.py`** — diff the curated catalog against the live
backend (curated collections no longer served, recipe processes no longer
advertised, untracked live collections):

```bash
python tools/openeo/audit_openeo_datasets.py audit
python tools/openeo/audit_openeo_datasets.py audit --strict   # exit 1 on any drift
```

**`earthlens datasets probe openeo <id>`** — inspect one collection's live
metadata when curating a new row: the band schema plus one `dim:<axis>` row
per non-band cube axis (the spatial bbox / temporal interval):

```bash
earthlens datasets probe openeo SENTINEL2_L2A
earthlens datasets probe openeo ESA_WORLDCOVER_10M_2021_V2 --json
```

`validate-recipe` and `audit --strict` exit non-zero on any drift (a base
collection or a process the backend no longer advertises).

## Gotchas & limits

* **Sync size cap** — large requests fail the sync endpoint; switch to
  `execute="batch"`.
* **OIDC in CI** — the interactive flow needs a browser once; CI must use
  client-credentials or a cached refresh token (see [Authentication](authentication.md)).
* **Recipe bands** — overriding a recipe's bands can break its graph (e.g. the
  NDVI recipe needs `B04`/`B08`/`SCL`); prefer leaving the band list empty.
* **MVP scope** — arbitrary user-supplied process graphs / UDFs are a follow-on;
  the MVP ships the curated recipe library plus plain-collection loads.
