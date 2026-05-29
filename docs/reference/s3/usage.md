# Amazon S3 — how to download

Every dataset uses the **same call shape**. You pick a `dataset`, list
`variables` (friendly names, aliases, or raw native tokens), give a lat/lon
bbox and a date window, and call `download()`. The result is a `list[Path]` of
files cropped to your AOI.

## The request

```python
from earthlens.s3 import S3

src = S3(
    start="2024-06-01", end="2024-06-06",   # inclusive date window
    lat_lim=[30.00, 30.06],                 # [lat_min, lat_max]
    lon_lim=[31.20, 31.26],                 # [lon_min, lon_max]
    dataset="sentinel-2-l2a",
    variables=["red", "nir"],               # friendly aliases -> B04, B08
    path="out/",
)
paths = src.download()                      # cropped GeoTIFFs under out/
```

### Constructor arguments

| Argument | Meaning |
|---|---|
| `dataset` | A registered name (`"era5"`, `"sentinel-2-l2a"`, `"goes"`, `"copernicus-dem"`, `"esa-worldcover"`) **or** an inline spec dict (passthrough). |
| `variables` | Variable / band tokens — friendly names, aliases, or raw native tokens. `None` uses the dataset's defaults. |
| `lat_lim` / `lon_lim` | The AOI bbox in degrees (EPSG:4326). |
| `start` / `end` / `fmt` | Inclusive date window (ignored for static datasets like DEM / WorldCover). |
| `temporal_resolution` | `"monthly"` (default) or `"daily"` — date stepping for temporal datasets. |
| `bucket` | Override the dataset's bucket (e.g. `"copernicus-dem-90m"`, `"noaa-goes18"`). |
| `output_format` | `"geotiff"` to convert output to GeoTIFF (NetCDF datasets always emit GeoTIFF after cropping). |
| `aws_profile` | Optional signed profile (default unsigned). |
| `path` | Output directory. |

### Discover what's available

```python
>>> from earthlens.s3 import S3
>>> S3.datasets()
['copernicus-dem', 'era5', 'esa-worldcover', 'goes', 'sentinel-2-l2a']
```

### Dry-run (what would I download?)

`_search()` plans the S3 keys without transferring data:

```python
>>> src = S3(start="2021-01-01", end="2021-01-01",
...          lat_lim=[0.4, 0.6], lon_lim=[6.4, 6.6], dataset="copernicus-dem")
>>> [p.href for p in src._search()]
['Copernicus_DSM_COG_10_N00_00_E006_00_DEM/Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif']
```

## The passthrough (any public bucket)

An unregistered dataset is supplied as an **inline spec dict with the same
fields** a catalog row uses — invocation is identical to a registered dataset:

```python
src = S3(
    start="2024-01-01", end="2024-01-01",
    lat_lim=[0, 1], lon_lim=[0, 1],
    dataset={
        "bucket": "my-open-bucket",
        "format": "cog",
        "layout": "deterministic_tiles",
        "crs": 4326,
        "params": {"key_template": "{year}/tile_{variable}.tif"},
    },
    variables=["band1"],
)
```

The `key_template` may reference `{variable}`, `{year}`, `{month}`, `{day}`,
and `{doy}`.

## Cropping, reprojection, and output

- **WGS84 datasets** (ERA5, Copernicus DEM, ESA WorldCover) are cropped directly
  to the bbox; ERA5's 0–360 longitudes are wrapped to −180..180.
- **Per-file-CRS COGs** (Sentinel-2 UTM) are reprojected to WGS84, then cropped.
- **Multi-tile AOIs** (DEM / WorldCover spanning more than one tile) yield **one
  cropped file per tile**; merging them into a single mosaic is a follow-on
  (`PY-1`, a pyramids capability).
- **Output**: COG datasets and (cropped) NetCDF datasets write GeoTIFFs; NetCDF
  time steps become raster bands.

## Aggregation (`aggregate=`)

NetCDF datasets (currently ERA5) accept an
`earthlens.aggregate.AggregationConfig` to emit per-window reductions:

```python
from earthlens.aggregate import AggregationConfig

src = S3(start="2023-12-01", end="2023-12-31", lat_lim=[40, 42], lon_lim=[12, 14],
         dataset="era5", variables=["t2m"], path="out/")
windows = src.download(aggregate=AggregationConfig(freq="D", op="mean"))
```

Aggregation runs on the raw granule's time axis and writes per-window GeoTIFFs to
`aggregate.out_dir` (or `<path>/aggregated`). COG datasets reject `aggregate=`
with `NotImplementedError`.

## Known limitations

- **GOES** (geostationary NetCDF) downloads, but cropping/reprojection to WGS84
  is deferred to the pyramids `PY-1` port; `_localise` raises a clear
  `NotImplementedError` until it lands.
- **Multi-tile mosaic** is not yet merged (one file per tile) — `PY-1`.
