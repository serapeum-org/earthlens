# EUMETSAT Data Store — introduction

`earthlens.eumetsat` is one unified backend over the **EUMETSAT Data
Store**, the access point for EUMETSAT's satellite archive. A single
OAuth2 consumer key / secret unlocks every collection the Data Store
serves — roughly **181 collections** across eight mission families:

| Group | Coverage |
|-------|----------|
| **MTG** | Meteosat Third Generation — FCI imager, Lightning Imager |
| **MSG** | Meteosat Second Generation — SEVIRI L1.5, cloud products |
| **Metop** | EPS — ASCAT scatterometer, IASI sounder, AVHRR / GOME-2 / AMSU / MHS |
| **Metop-SG** | Metop Second Generation (new instruments) |
| **Sentinel-3** | OLCI / SLSTR / SRAL marine mirror |
| **Sentinel-5P** | TROPOMI atmospheric composition mirror |
| **Sentinel-6** | Poseidon-4 altimetry mirror |
| **OSI-SAF** | Ocean & Sea Ice — sea-ice concentration, SST |

The backend wraps the official
[`eumdac`](https://pypi.org/project/eumdac/) client: one
consumer-key/secret pair mints an auto-refreshing bearer token, and every
collection is searched by bounding box + time window and **fetched as a
whole native product to disk**.

## Output kind

Like the NASA Earthdata backend, EUMETSAT's `OUTPUT_KIND` is
**per-collection, not fixed**. Each catalog row carries an
`output_kind` (`raster` / `vector` / `tabular`) that the backend copies
onto the instance at construction. The curated MVP collections are all
gridded / griddable products, so they are `raster`; a request that mixes
output kinds is rejected up front.

## How it maps onto the `EarthLens` facade

```python
from earthlens.earthlens import EarthLens

el = EarthLens(
    data_source="eumetsat",
    start="2024-06-01",
    end="2024-06-01",
    variables={"msg-hrseviri": ["HRSEVIRI"]},
    lat_lim=[0.0, 10.0],
    lon_lim=[0.0, 10.0],
    path="eumetsat_output",
)
paths = el.download()   # native products on disk
```

`variables` is a `{collection_key: [selector, ...]}` mapping (see
[Usage](usage.md)); the bbox is a **search filter** (which products
intersect it), not a pixel crop.

## Installation

The backend's SDK is an optional extra. The package imports without it;
the `eumdac` import is lazy and only happens when you actually
authenticate or download.

```bash
pip install earthlens[eumetsat]
```

## What's in the MVP — and what's deferred

The MVP **fetches native products**. Two capabilities are deferred
follow-ons:

* **Data Tailor** (server-side subset / reproject / format-convert) — the
  `aggregate=` / bbox-crop path. `download(aggregate=...)` raises
  `NotImplementedError` naming it. See [Data Tailor](data-tailor.md).
* **Native SEVIRI / FCI client-side reading** — needs a satpy reader
  bridge in `pyramids`. NetCDF mirror products (Sentinel-3 / -5P / -6,
  OSI SAF) are readable with `pyramids` today.

See [Authentication](authentication.md), [Usage](usage.md), and the
[Catalog & tooling](catalog.md) reference for the full picture.
