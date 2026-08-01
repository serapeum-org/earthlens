# EUMETSAT Data Store — introduction

<img src="../../_images/logos/eumetsat.svg" alt="EUMETSAT logo" height="60">

`earthlens.eumetsat` is one unified backend over the **EUMETSAT Data
Store**, the access point for EUMETSAT's satellite archive. A single
OAuth2 consumer key / secret unlocks every collection the Data Store
serves. The catalog curates the **entire Data Store — all ~180
collections** — grouped into ten mission families:

| Group | Coverage |
|-------|----------|
| **MTG** | Meteosat Third Generation — FCI imager, Lightning Imager, FCI L2 suite |
| **MSG** | Meteosat Second Generation — SEVIRI L1.5, cloud products, CDRs |
| **MFG** | Meteosat First Generation — MVIRI / GSA climate data records |
| **Metop** | EPS — ASCAT scatterometer, IASI sounder, AVHRR / GOME-2 / AMSU / MHS, CDR/FDR |
| **Metop-SG** | Metop Second Generation (GRAS-2, auxiliary, BUFR) |
| **Sentinel-3** | OLCI / SLSTR / SRAL marine mirror (NRT + reprocessed baselines) |
| **Sentinel-5P** | TROPOMI atmospheric composition mirror |
| **Sentinel-6** | Poseidon-4 altimetry + microwave radiometer mirror |
| **OSI-SAF** | Ocean & Sea Ice — sea-ice concentration, SST |
| **Other** | Multimission / legacy climate & fundamental data records (ATMS, MWHS, SSM/T, HIRS, CM SAF, SARAH, AWS, RO) |

The backend wraps the official
[`eumdac`](https://pypi.org/project/eumdac/) client: one
consumer-key/secret pair mints an auto-refreshing bearer token, and every
collection is searched by bounding box + time window and **fetched as a
whole native product to disk**.

## Output kind

Like the NASA Earthdata backend, EUMETSAT's `OUTPUT_KIND` is
**per-collection, not fixed**. Each catalog row carries an
`output_kind` (`raster` / `vector` / `tabular`) that the backend copies
onto the instance at construction. Most collections are gridded
(`raster`); the point/vector products — Atmospheric Motion Vectors,
ASCAT winds, and the Lightning Imager event/flash/group products — are
tagged `vector`. A request that mixes output kinds is rejected up front.

## How it maps onto the `EarthLens` facade

```python
from earthlens.core import EarthLens

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

## Capabilities

The backend **fetches native products** by default, and supports EUMETSAT
**Data Tailor** for server-side customisation:

* **Data Tailor** (server-side subset / reproject / reformat) — the
  `tailor=TailorConfig(...)` knob. Returns the customised GeoTIFF / NetCDF.
  See [Data Tailor](data-tailor.md). (`aggregate=` is the separate,
  unimplemented *temporal* reducer and raises `NotImplementedError`.)
* **Native SEVIRI / FCI client-side reading** — needs a satpy reader
  bridge in `pyramids` (a deferred follow-on); tailoring to GeoTIFF
  sidesteps it. NetCDF mirror products (Sentinel-3 / -5P / -6, OSI SAF)
  are readable with `pyramids` today.

See [Authentication](authentication.md), [Usage](usage.md), and the
[Catalog & tooling](catalog.md) reference for the full picture.
