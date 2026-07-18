# Migrating from Google Earth Engine to `earthlens.stac`

Most workloads that load imagery from a single Earth Engine asset id (`ee.ImageCollection("…")`)
map directly to one of `earthlens.stac`'s eight curated endpoints. The two backends speak different
APIs — Earth Engine is a server-side processing language; `earthlens.stac` is a STAC-API
client that hands you Cloud-Optimized GeoTIFFs — but the catalogue overlap is broad enough that
the top ten GEE asset ids each have an `earthlens.stac` equivalent. This page lists them.

## When to use which backend

* **You want pixels on disk over your AOI/window** → `earthlens.stac`. One COG per
  `(collection, date)`, anonymous reads on most endpoints (Earth Search, Digital Earth Africa /
  Australia, NASA VEDA, Brazil Data Cube, Microsoft Planetary Computer), requester-pays on
  `usgs-landsat`, S3 keys on `cdse`.
* **You want a server-side reduction** (e.g. composites, mosaics, indices computed on EE's
  cluster) → `earthlens.openeo` (the openEO process-graph backend). See
  [`Dynamic World`](#server-side-reductions-google-dynamicworld-v1) below.
* **You want GEE-only assets** (e.g. Google's proprietary atmospheric correction, internal LST
  products) → keep `earthlens.gee`; nothing in `earthlens.stac` substitutes for it.

## Top-10 GEE → `earthlens.stac` mappings

| GEE asset id | `earthlens.stac` endpoint / collection | Notes |
|---|---|---|
| `COPERNICUS/S2_SR_HARMONIZED` | `planetary-computer / sentinel-2-l2a` (or `earth-search / sentinel-2-l2a`) | Same MGRS scenes; Earth Search is fully anonymous, MPC mints a SAS token. |
| `LANDSAT/LC08/C02/T1_L2` | `usgs-landsat / landsat-c2l2-sr` | Authoritative USGS source; requester-pays on `s3://usgs-landsat`. MPC's `landsat-c2-l2` is a mirror. |
| `LANDSAT/LC09/C02/T1_L2` | `usgs-landsat / landsat-c2l2-sr` | LC08 and LC09 share the SR product; filter by `platform` in the search. |
| `NASA/HLS/HLSL30/v002` | `planetary-computer / hls2-l30` | Harmonised Landsat (30 m). MPC handles the EDL auth so you don't need an Earthdata account. |
| `NASA/HLS/HLSS30/v002` | `planetary-computer / hls2-s30` | Harmonised Sentinel-2 (30 m). |
| `MODIS/061/MOD13Q1` | `bdc / mod13q1-6.1` | 16-day NDVI/EVI; BDC reads anonymously today. |
| `MODIS/061/MYD13Q1` | `bdc / myd13q1-6.1` | 16-day NDVI/EVI (Aqua). |
| `COPERNICUS/DEM/GLO30` | `planetary-computer / cop-dem-glo-30` (or `earth-search / cop-dem-glo-30`) | 30 m global DEM, anonymous. |
| `ESA/WorldCover/v200` | `planetary-computer / esa-worldcover` | 2021 global 10 m land cover. |
| `USGS/3DEP/10m` | `planetary-computer / 3dep-seamless` | USGS 10 m DEM, US-only coverage. |

For DEM specifically, `earth-search/cop-dem-glo-90` and `deafrica/dem_cop_30` / `dem_cop_90` are
also valid anonymous mirrors with different default extents.

## Runnable snippets

Each snippet replaces the equivalent `ee.ImageCollection(...).filterBounds(...).filterDate(...)
.first()` recipe in Earth Engine and writes a Cloud-Optimized GeoTIFF.

### `COPERNICUS/S2_SR_HARMONIZED` → MPC sentinel-2-l2a (RGB)

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="planetary-computer",
    variables={"sentinel-2-l2a": ["B04", "B03", "B02"]},
    lat_lim=[40.40, 40.45], lon_lim=[-3.72, -3.67],
    start="2024-06-01", end="2024-06-20",
    path="./out", max_items=1,
).download()
```

### `LANDSAT/LC08/C02/T1_L2` → USGS LandsatLook (requester-pays)

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="usgs-landsat",        # alias 'landsat' works too
    variables={"usgs-landsat/landsat-c2l2-sr": ["red", "green", "blue", "nir08"]},
    lat_lim=[37.5, 38.0], lon_lim=[-122.5, -122.0],
    start="2024-06-01", end="2024-08-31",
    path="./out", max_items=1,
).download()
```

USGS LandsatLook reads from `s3://usgs-landsat` (requester-pays) — make sure
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are set.

### `NASA/HLS/HLSL30/v002` → MPC hls2-l30

```python
paths = EarthLens(
    data_source="planetary-computer",
    variables={"hls2-l30": ["B04", "B05"]},   # red + nir for NDVI
    lat_lim=[40.40, 40.45], lon_lim=[-3.72, -3.67],
    start="2024-06-01", end="2024-08-31",
    path="./out", max_items=1,
).download()
```

### `MODIS/061/MOD13Q1` → BDC mod13q1-6.1 (16-day NDVI, anonymous)

```python
paths = EarthLens(
    data_source="bdc",                  # alias 'brazil-data-cube' works too
    variables={"bdc/mod13q1-6.1": ["NDVI"]},
    lat_lim=[-23.7, -23.2], lon_lim=[-46.8, -46.3],
    start="2024-01-01", end="2024-12-31",
    path="./out", max_items=1,
).download()
```

### `COPERNICUS/DEM/GLO30` → MPC cop-dem-glo-30 (DEM, anonymous)

```python
paths = EarthLens(
    data_source="planetary-computer",
    variables={"cop-dem-glo-30": ["data"]},
    lat_lim=[40.40, 40.45], lon_lim=[-3.72, -3.67],
    start="2020-01-01", end="2024-12-31",
    path="./out", max_items=1,
).download()
```

### `ESA/WorldCover/v200` → MPC esa-worldcover (10 m land cover)

```python
paths = EarthLens(
    data_source="planetary-computer",
    variables={"esa-worldcover": ["map"]},
    lat_lim=[40.40, 40.45], lon_lim=[-3.72, -3.67],
    start="2021-01-01", end="2021-12-31",
    path="./out", max_items=1,
).download()
```

### `USGS/3DEP/10m` → MPC 3dep-seamless (10 m DEM, US only)

```python
paths = EarthLens(
    data_source="planetary-computer",
    variables={"3dep-seamless": ["data"]},
    lat_lim=[37.5, 38.0], lon_lim=[-122.5, -122.0],
    start="2020-01-01", end="2024-12-31",
    path="./out", max_items=1,
).download()
```

## Server-side reductions: `GOOGLE/DYNAMICWORLD/V1`

There is no STAC equivalent for Dynamic World — it is a server-side ML reduction (run on Google's
cluster) rather than a downloadable raster catalogue. The equivalent in `earthlens` is the
process-graph backend:

```python
paths = EarthLens(
    data_source="openeo",
    variables={"DYNAMIC_WORLD": [...]},   # see earthlens.openeo
    ...
).download()
```

Same applies to other GEE-resident reducers — composite-on-the-fly, cloudless mosaics, server-side
spectral indices. Use `earthlens.openeo` (or stay on `earthlens.gee`) when the workload is the
reduction, not the raster.

## What does not migrate

The following GEE assets have no `earthlens.stac` equivalent today:

* Google's proprietary cloud-masking / atmospheric-correction layers (e.g. `COPERNICUS/S2_CLOUD_PROBABILITY`).
  Use the QA bands shipped with the STAC products (e.g. Sentinel-2 `SCL`, Landsat `qa_pixel`).
* Region-of-interest reducers (`ee.Reducer.mean()` etc.). Use `earthlens.openeo` for those, or
  pull the COGs and reduce locally with pyramids.
* `GOOGLE/*` (Dynamic World, Open Buildings, Floods). See the openEO note above; Open Buildings
  is also available as a flat-S3 release that an `earthlens.s3`-style dataset row can serve.

## See also

* [Introduction](introduction.md) — the eight endpoints + when each one wins.
* [Usage](usage.md) — the request shape (`variables={collection: [asset]}`, `aggregate=`, antimeridian).
* [Authentication](authentication.md) — the auth model per endpoint (anonymous / SAS / S3 keys /
  requester-pays / EDL bearer).
