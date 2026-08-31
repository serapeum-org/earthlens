# Copernicus DEM — introduction

<img src="../../_images/logos/dem.svg" alt="Copernicus DEM (ESA) logo" height="60">

earthlens ships a first-class `dem` backend for **global land elevation**
data. It targets the public **[Copernicus DEM](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model)**
GLO-30 (~30 m) and GLO-90 (~90 m) grids on the anonymous AWS Open Data
buckets — no account, no SDK login, no API key.

For the hands-on walkthrough see [Usage](usage.md), the two dataset ids
on the [Available datasets](datasets.md) page, and the rendered API on
the [Reference](dem.md) page.

## Why the `dem` backend exists

Copernicus DEM is already reachable through several shipped backends —
`gee`, `stac` (Planetary Computer / Earth Search / CDSE), `earthdata` —
**but every one of those paths needs an account** (a Google Earth Engine
service account, an Earthdata Login, or a STAC signer). This backend
earns its place by offering the same tiles with **zero credentials**.

The Copernicus DEM AWS buckets are anonymous and requester-free:

- `s3://copernicus-dem-30m` — GLO-30 (~30 m), region `eu-central-1`.
- `s3://copernicus-dem-90m` — GLO-90 (~90 m), region `eu-central-1`.

Every design choice below protects that account-free path: no new SDK
(reuses the shipped `[s3]` unsigned `boto3` substrate — the same client
`goes` / `nwm` / `radar` ride on), no signer, and no decode step that
would drag in a heavy stack.

## How it works

A request maps to the 1° x 1° COG tiles whose SW corner lies inside the
bounding box, one anonymous `head_object` per candidate, and one whole-
file `download_file` per tile that exists. `download()` returns the
`list[Path]` of downloaded COGs, in bbox row-major order.

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="dem",
    dataset="cop-dem-glo-30",
    lat_lim=[30.2, 30.8],
    lon_lim=[31.2, 31.8],
    path="dem_out",
).download()
# -> [PosixPath('dem_out/Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif')]
```

## What earthlens does — and doesn't do

- **Fetches raw COG tiles.** earthlens hands the tiles back as they came
  from the bucket. Cropping the bbox down to the exact AOI, mosaicking
  neighbouring tiles into a single raster, reprojecting to a UTM zone,
  and rendering a hillshade are all pyramids' job — its COG reader
  (`pyramids.Dataset.read_file`) plus `.crop` / `.to_crs` and
  `pyramids.dataset.merge.merge_rasters` (for a multi-tile mosaic)
  already do this. earthlens does **not** import `rasterio`, `osgeo`,
  or `xarray` — every raster read goes through pyramids.

- **1° tile granularity.** The buckets serve one COG per 1° x 1° tile,
  so a coastal bbox spanning several tiles yields one file per tile. To
  merge them, hand the list of paths to
  `pyramids.dataset.merge.merge_rasters(paths, out_path)`.

- **Ocean tiles are absent — logged and skipped.** Copernicus DEM ships
  **no tile over open ocean**. A bbox that crosses a coast produces a
  ragged coverage; the missing tiles are logged at WARNING and the
  download continues. See the accompanying notebook for a Nile Delta
  walkthrough that surfaces this cleanly.

- **DEM is time-invariant, so `aggregate=` is rejected.** A DEM has no
  time axis to reduce over. Passing a non-`None` `aggregate=` raises
  `NotImplementedError`.

- **Land only.** For sea-floor bathymetry (GEBCO / ETOPO1) use the
  separate `bathymetry` backend.

## Two ways to select the resolution

The `dataset=` kwarg picks the bucket:

- `dataset="cop-dem-glo-30"` (default) → the 30 m bucket, tile-name
  token `10`.
- `dataset="cop-dem-glo-90"` → the 90 m bucket, tile-name token `30`.

Or use one of the discoverability aliases (`copernicus-dem`,
`cop-dem`) or the topic key `dem:elevation` — they all route to the same backend.

## Attribution

Copernicus DEM is **free to use with attribution**:

> © ESA — produced from Copernicus data.

The per-tile `INFO/eula_F.pdf` (shipped alongside every tile in the
bucket) is the ESA end-user licence. earthlens does not emit a runtime
`LicenseWarning` for Copernicus DEM — please honour the attribution in
downstream publications.
