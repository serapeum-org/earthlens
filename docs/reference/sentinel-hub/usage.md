# Sentinel Hub — usage

## The request shape

A Sentinel Hub request is `variables={collection_or_recipe: [band, ...]}` plus a
bbox (`lat_lim` / `lon_lim`) and a date window (`start` / `end`). A `variables`
**key** is either:

- an **evalscript recipe** (e.g. `"sentinel-2-l2a-ndvi"`) — a bundled `.js` that
  pins its collection and render logic; the band list may be empty, or
- a **collection** (e.g. `"sentinel-2-l2a"`) — then you must pass an explicit
  `evalscript=` (an inline V3 JS string or a `.js` file path).

See the [collections & recipes reference](datasets.md) for the full bundled
library.

```python
from earthlens import EarthLens

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-10", end="2020-06-20",
    variables={"sentinel-2-l2a-ndvi": []},
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
)
paths = el.download()       # -> [Path('out/.../response.tiff')]
```

## Keyword arguments

| Keyword | Default | Meaning |
|---|---|---|
| `resolution` | `10.0` | Output pixel size in metres (drives `bbox_to_dimensions`). |
| `evalscript` | `None` | A custom evalscript — inline V3 JS string or a `.js` path — that bypasses the recipe lookup. |
| `endpoint` | `"cdse"` | `"cdse"` / `"commercial"` / a full base URL. |
| `mosaicking_order` | `"mostRecent"` | Per-pixel scene selection: `"mostRecent"` / `"leastRecent"` / `"leastCC"`. |
| `api` | `None` | The request plane (see below); `None` auto-selects. |
| `geometry` | `None` | A shapely geometry / GeoJSON mapping / `FeatureCollection` for the Statistical planes. |
| `maxcc` | `None` | Maximum cloud cover (0–1), forwarded to `input_data` (optical only). |
| `batch_output` | `None` | S3 delivery spec for the async / batch planes (see below). |
| `client_id` / `client_secret` / `profile` | `None` | Credentials (see [Authentication](authentication.md)). |

`download()` returns the **written GeoTIFF paths** (raster planes), the **table
paths** (statistical planes), or the **S3 destination URIs** (async / batch).

## The request planes (`api=`) and size auto-routing

| `api=` | Output | Notes |
|---|---|---|
| `"process"` | GeoTIFF | Synchronous, ≤ 2500 px/side. |
| `"async"` | S3 URIs | ≤ 10000 px/side; needs `batch_output`. |
| `"tiling"` | GeoTIFF | Split into ≤ 2500 px tiles + mosaic; no S3. |
| `"batch"` | S3 URIs | Server-side tiling to S3; needs `batch_output`. |
| `"statistical"` | CSV | Zonal stats; needs `geometry=`. |
| `"batch-statistical"` | CSV | Zonal stats over a huge `FeatureCollection` via S3. |

When `api=` is omitted, the backend computes the render size from the bbox +
`resolution` and routes:

```
geometry= supplied                 -> statistical
size ≤ 2500 px                     -> process
size > 2500 px, no batch_output    -> tiling   (local split + mosaic)
size 2500–10000 px, batch_output   -> async    (S3)
size > 10000 px, batch_output      -> batch    (S3)
```

An explicit `api=` is honoured verbatim; forcing `api="process"` on an oversized
request raises a clear error rather than silently truncating.

!!! note "Async is S3-delivered"
    Sentinel Hub's Async Processing API delivers to S3 (it is not a direct
    synchronous download). The **no-S3** path for medium/large AOIs is therefore
    `"tiling"` — local split into Process tiles, mosaicked with
    `pyramids.dataset.merge.merge_rasters`.

## A custom evalscript

```python
NDVI = """//VERSION=3
function setup() { return { input: ["B04","B08"], output: { bands: 1, sampleType: "FLOAT32" } }; }
function evaluatePixel(s) { return [(s.B08 - s.B04) / (s.B08 + s.B04)]; }
"""

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-10", end="2020-06-20",
    variables={"sentinel-2-l2a": []},     # a plain collection
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
    evalscript=NDVI,                       # inline JS (or a path to a .js file)
)
```

## `aggregate=` — windowed reduction

`download(aggregate=AggregationConfig(freq=..., op=...))` is accepted on every
plane (the backend is `OUTPUT_KIND="mixed"`, so the facade forwards it):

- **raster planes** loop one render per `freq` window, writing one per-window
  GeoTIFF named `{key}_{freq}_{YYYYMMDD}.tif` (the `ecmwf` / `cmems` shape);
- the **statistical plane** maps `freq` to the Statistical `aggregation_interval`
  (the server returns one stats row per interval).

```python
from earthlens.aggregate import AggregationConfig

el.download(aggregate=AggregationConfig(freq="1MS", op="mean"))  # monthly windows
```

## Browsing coverage with the Catalog API

`backend.search()` queries the Sentinel Hub Catalog API for the scenes
intersecting the request bbox + window, without rendering:

```python
el = EarthLens(data_source="sentinel-hub", ...)
products = el.datasource.search(limit=50)
for p in products:
    print(p.id, p.metadata["datetime"])
```

## Rate limits & quotas

Sentinel Hub enforces per-account **processing-unit** quotas. `sentinelhub-py`
retries throttled requests automatically (its `default_retry_time` is 30 s in
3.11.x). Keep AOIs and windows tight, prefer the Statistical plane for
zonal questions (it is far cheaper than rendering), and use Batch for
continental work.
