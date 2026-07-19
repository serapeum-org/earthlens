# openEO backend — introduction

<img src="../../_images/logos/openeo.png" alt="openEO logo" height="60">

`earthlens.openeo` is a **server-side processing** backend. Unlike the
fetch-to-disk backends (which download granules and process them locally), it
builds an [openEO](https://openeo.org) **process graph** — a portable JSON DAG
of `load_collection → mask → reduce → save_result` — the **backend executes it
server-side**, and earthlens downloads the gridded result.

Its closest sibling is the Google Earth Engine backend: the *server* does the
compute (band math, cloud masking, temporal reduction, reprojection) and
earthlens just (a) authenticates, (b) builds the graph from a curated catalog,
(c) triggers execution, and (d) collects the written output. The difference from
GEE: openEO is **provider-agnostic and open source**, the graph is portable
JSON, and `aggregate=` is a **native** openEO process
(`aggregate_temporal_period`) rather than a client-side post-step.

```bash
pip install earthlens[openeo]
```

> **Dependency note.** earthlens pins `openeo >=0.47,<0.52` (which resolves to
> 0.51). openeo 0.51 caps **both** `xarray<2025.01.2` and `pandas<3.0.0`, so
> installing `earthlens[openeo]` (or `earthlens[all]`) constrains `xarray` and
> holds the environment to **pandas 2.x** — pandas 3 is published, but openeo's
> `<3` cap excludes it.

## Endpoints

The backend defaults to **CDSE openEO** (free with a Copernicus Data Space
account). The `endpoint=` kwarg selects another federation:

| `endpoint=` | URL | Notes |
|---|---|---|
| `cdse` *(default)* | `https://openeo.dataspace.copernicus.eu` | CDSE core; free with a CDSE account |
| `cdse-federation` | `https://openeofed.dataspace.copernicus.eu` | the CDSE-hosted federation |
| `openeo-platform` | `https://openeo.cloud` | the openEO Platform federation (NoR-sponsored) |
| *any* `https://…` | a custom URL | used verbatim |

These are the **same CDSE account** as the STAC `cdse` endpoint, but a different
auth plane — OIDC tokens here vs S3 keys there (see
[Authentication](authentication.md)).

## The two-layer catalog

openEO is compute, not fetch, so the "catalog" has two layers (see
[Collections & recipes](datasets.md) for the full reference):

* **Collections** — the underlying CDSE openEO collections
  (`SENTINEL2_L2A`, `SENTINEL1_GRD`, `SENTINEL_5P_L2`, …) with their bands. A
  request that names a collection gets the default graph
  (`load_collection → clip → save`).
* **Recipes** — a curated library of named **process graphs**
  (`sentinel-2-l2a-ndvi-monthly`, `sentinel-3-olci-chlorophyll-monthly`, …),
  each fixing a base collection + bands + an ordered list of steps (mask →
  reduce → save).

A request names **either** a collection (plus optional bands) **or** a recipe:

```python
from earthlens.earthlens import EarthLens

# A recipe — its fixed graph runs server-side:
EarthLens(
    data_source="openeo",
    variables={"sentinel-2-l2a-ndvi-monthly": []},
    start="2023-06-01", end="2023-08-31",
    lat_lim=[40.40, 40.45], lon_lim=[3.67, 3.72],
    path="out",
).download()

# A collection — the default load → clip → save graph, with chosen bands:
EarthLens(
    data_source="openeo",
    variables={"sentinel-2-l2a": ["B04", "B08"]},
    start="2023-06-01", end="2023-06-10",
    lat_lim=[40.40, 40.45], lon_lim=[3.67, 3.72],
    path="out",
).download()
```

## What it returns

Gridded **`raster`** output: the server writes one GeoTIFF / NetCDF per
requested key to your `path`, and `download()` returns the list of written file
paths. Because the output is raster, the `EarthLens` facade forwards a
`aggregate=AggregationConfig(...)` to the backend — where it becomes a native
`aggregate_temporal_period` node in the graph (see [Usage](usage.md)).

## How it is built

earthlens owns only the **provider glue**: the `openeo` connection + OIDC auth
(`earthlens.openeo.auth`), the two-layer catalog (`earthlens.openeo.catalog`),
the request → process-graph translation and sync-vs-batch execution
(`earthlens.openeo.backend`), and the catalog tooling
(`tools/openeo/refresh_openeo_catalog.py`). There is **no pyramids
prerequisite** — like GEE, the server produces the raster and earthlens
downloads it; pyramids is only touched if you post-process the result locally.
