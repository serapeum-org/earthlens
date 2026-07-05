# Copernicus DEM — usage

The `dem` backend downloads Copernicus DEM COG tiles for a bbox — no
credentials, no SDK login, no key. Every request returns the raw 1°
tiles it fetched; cropping / mosaicking / reprojecting is done downstream
in [pyramids](https://github.com/serapeum-org/pyramids).

See [Available datasets](datasets.md) for the two `dataset=` ids and
[Introduction](introduction.md) for the design rationale.

## A GLO-30 request over one tile

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="dem",
    dataset="cop-dem-glo-30",         # default
    lat_lim=[30.2, 30.8],             # [south, north]
    lon_lim=[31.2, 31.8],             # [west, east]
    path="dem_out",
).download()

paths     # [PosixPath('dem_out/Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif')]
```

`download()` returns the list of downloaded COGs in row-major
(south → north, west → east) order. Each file is a native Copernicus
tile — do not expect it to be already cropped to the bbox; the tile
covers a full 1° x 1° square.

## Reading a tile back with pyramids

Copernicus DEM COGs carry a WGS84 CRS. Read them straight into
`pyramids.Dataset`:

```python
from pyramids.dataset import Dataset

dem = Dataset.read_file(str(paths[0]))
dem.epsg              # 4326 (WGS84)
array = dem.read_array()   # elevation in metres (EGM2008 vertical datum)
```

Copernicus DEM's vertical datum is **EGM2008**, and the horizontal datum
is **WGS84** — the values are height above the EGM2008 geoid, not
above the WGS84 ellipsoid. For orthometric analysis this is what you
want; if you need ellipsoidal heights, add the geoid separation.

## Cropping the tile to the exact bbox

`download()` returns the whole 1° tile. To keep only the bbox pixels:

```python
from pyramids.dataset import Dataset

dem = Dataset.read_file(str(paths[0]))
cropped = dem.crop([31.2, 30.2, 31.8, 30.8])  # [west, south, east, north]
```

## Mosaicking neighbouring tiles

A wider bbox returns several files — one per intersected tile.
`pyramids.dataset.merge.merge_rasters` stitches them into a single COG:

```python
from pyramids.dataset import Dataset
from pyramids.dataset.merge import merge_rasters

paths = EarthLens(
    data_source="dem",
    lat_lim=[45.2, 46.8],   # spans two lat tiles
    lon_lim=[7.2, 8.8],     # spans two lon tiles
    path="alps_out",
).download()

mosaic_path = "alps_out/alps_mosaic.tif"
merge_rasters([str(p) for p in paths], mosaic_path)
mosaic = Dataset.read_file(mosaic_path)  # single continuous WGS84 raster
```

## Coastal bbox — some tiles do not exist

Copernicus DEM ships **no tile over open ocean**. A bbox that spans the
coast produces a ragged coverage; the missing tiles are logged at
WARNING and the download proceeds:

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="dem",
    lat_lim=[43.0, 44.0],
    lon_lim=[-6.0, -4.0],    # Bay of Biscay + Cantabrian coast
    path="coast_out",
).download()

# `paths` holds only the land tiles; the ocean squares are absent from
# the bucket by design, and each surfaces as a loguru WARNING:
#     dem: tile absent, skipping: s3://copernicus-dem-30m/…
```

## Picking GLO-30 vs. GLO-90

Use `dataset=` to switch resolution — the file names carry a different
token, so files from the two datasets never collide:

```python
# GLO-30 (~30 m) — token "10"
paths_30 = EarthLens(data_source="dem", dataset="cop-dem-glo-30", ...).download()
# GLO-90 (~90 m) — token "30"
paths_90 = EarthLens(data_source="dem", dataset="cop-dem-glo-90", ...).download()
```

GLO-90 files are ~10 x smaller than GLO-30 files, which is helpful for
continental-scale surveys where the finer resolution is not needed.

## What the backend doesn't do

- **No server-side spatial subset.** The buckets serve whole 1° COGs —
  a bbox smaller than a tile still downloads the whole tile.
- **No decode.** earthlens hands the file back; reading, reprojection,
  and mosaicking are pyramids' job (see the [quickstart notebook](../../examples/dem/01_dem_quickstart.ipynb)).
- **No `aggregate=`.** DEM has no time axis to reduce over — passing a
  non-`None` `aggregate=` raises `NotImplementedError`.
- **No sea-floor bathymetry.** Use the separate `bathymetry` backend
  for GEBCO / ETOPO1.
