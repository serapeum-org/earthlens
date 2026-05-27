# Sentinel Hub — introduction

`earthlens.sentinel_hub` is a **server-side render** backend over
[Sentinel Hub](https://www.sentinel-hub.com/) on the **Copernicus Data Space
Ecosystem** (CDSE, `sh.dataspace.copernicus.eu` — free with a CDSE account).
earthlens sends a **bounding box / geometry + time window + an evalscript** (a
small JavaScript band-math program) to one of Sentinel Hub's request planes; the
**server computes on-the-fly** and earthlens collects the result.

It is the closest sibling of the [GEE](../gee/introduction.md) and
[openEO](../openeo/introduction.md) backends: the server does the band math,
compositing, and rendering, and earthlens (a) authenticates, (b) builds the
request from a curated **evalscript library**, (c) triggers, and (d) collects the
output.

## What it returns — `mixed` output

The backend's `OUTPUT_KIND` is **`"mixed"`**: depending on the request plane it
emits either a **raster** (GeoTIFF) or a **tabular** file (CSV of zonal
statistics).

| Plane (`api=`) | Output | When |
|---|---|---|
| `process` | GeoTIFF | Synchronous raster, ≤ 2500 px/side |
| `async` | S3 URIs | Raster ≤ 10000 px/side, delivered to your S3 bucket |
| `tiling` | GeoTIFF | Oversized AOI split into ≤ 2500 px tiles + mosaicked locally (no S3) |
| `batch` | S3 URIs | Continental/global AOI, server-side tiling to your S3 bucket |
| `statistical` | CSV | Zonal statistics over a polygon / `FeatureCollection` |
| `batch-statistical` | CSV | Zonal stats over a huge `FeatureCollection` (async, via S3) |

When you omit `api=`, the backend **auto-selects** a plane from the request size,
whether a `geometry=` was supplied, and whether an S3 `batch_output` is
configured (see [Usage](usage.md)).

## Where it fits in the facade

```python
from earthlens import EarthLens

el = EarthLens(
    data_source="sentinel-hub",   # alias: "sentinelhub"
    start="2020-06-10", end="2020-06-20",
    variables={"sentinel-2-l2a-ndvi": []},
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
)
paths = el.download()
```

## Install

```bash
pip install earthlens[sentinel-hub]
```

This pulls the [`sentinelhub`](https://sentinelhub-py.readthedocs.io/) client
(`>=3.11.5`). The SDK is imported lazily, so `import earthlens` works without the
extra; a missing extra surfaces a friendly `ImportError` only when you actually
construct or download.

See [Authentication](authentication.md) for credentials,
[Usage](usage.md) for the request shape and every keyword, the
[collections & recipes reference](datasets.md) for the bundled evalscript
library, and the [Statistical & Batch guide](statistical-batch.md) for the
tabular and scale-out planes.
