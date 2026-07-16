# Bathymetry DEMs — introduction

<img src="../../_images/logos/bathymetry.png" alt="GEBCO logo" height="60">

earthlens ships a single `bathymetry` backend that fetches **global
topography / bathymetry digital elevation models (DEMs)** — continuous
grids of land height and sea-floor depth — subset on the server to a
requested bounding box and written as GeoTIFF. Two open data families are
covered:

- **[GEBCO](https://www.gebco.net/) 2020** — the General Bathymetric Chart
  of the Oceans, a 15-arc-second continuous terrain model spanning both
  land and ocean.
- **NOAA [ETOPO1](https://www.ncei.noaa.gov/products/etopo-global-relief-model)**
  — a 1-arc-minute global relief model, in two variants: the **Ice
  Surface** (top of the Antarctic / Greenland ice sheets) and the
  **Bedrock** (base of those ice sheets).

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md) and the dataset ids on the
[Available datasets](datasets.md) page; the rendered API is the
[Reference](bathymetry.md) page.

## How it works

Every DEM is reached through **one uniform transport**: a public NOAA
[ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/) `griddap` coverage.
A request builds the `griddap` bbox-subset URL, downloads the resulting
**NetCDF**, reads it with the
[pyramids](https://github.com/serapeum-org/pyramids) GIS backend
(`pyramids.netcdf.NetCDF`), extracts the elevation band, and writes a
**GeoTIFF** — so `download()` returns the list of written GeoTIFF paths
(`list[Path]`). earthlens never imports a competing array stack to touch
the NetCDF; the read goes through pyramids only.

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="bathymetry",
    dataset="gebco_2020",
    lat_lim=[25.0, 26.0],
    lon_lim=[-18.0, -17.0],
    path="bathy_out",
).download()
# -> [Path('bathy_out/gebco_2020.tif')]
```

## Two things that shape the backend

- **The DEMs are static — there is no time axis.** A DEM is a single
  global grid, not a time series, so `download()` has nothing to reduce
  over time. The `EarthLens` facade forwards `aggregate=` to raster
  backends, so the `bathymetry` backend explicitly rejects a non-`None`
  `aggregate=` with `NotImplementedError`. For gridded ocean fields you
  *do* want to aggregate, use the CMEMS backend instead.

- **A request is a bbox subset, not a whole-grid download.** The full
  GEBCO / ETOPO grids are multi-gigabyte global files; this backend only
  fetches the bbox you ask for. A bbox outside a DEM's coverage, or one so
  large the ERDDAP server rejects it, surfaces as a clear `ValueError`
  telling you to shrink the box — never a corrupt file or a bare HTTP
  error.

## Attribution

Both DEMs are open and free to use, with a courtesy attribution:

- **GEBCO** — GEBCO Compilation Group (2020) GEBCO 2020 Grid
  (doi:10.5285/a29c5465-b138-234d-e053-6c86abc040b9).
- **ETOPO1** — Amante, C. and B.W. Eakins, 2009. ETOPO1 1 Arc-Minute
  Global Relief Model (doi:10.7289/V5C8276M).

Both are served by the NOAA ERDDAP (U.S. Government public domain).
