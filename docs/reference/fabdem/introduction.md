# FABDEM — introduction

earthlens ships a `fabdem` backend that fetches **FABDEM V1-2** (Forest And
Buildings removed Copernicus DEM) — a global **bare-earth** digital elevation
model at ~30 m (1 arc-second) — subset to a requested bounding box and written
as GeoTIFF. FABDEM takes the Copernicus GLO-30 digital *surface* model and
removes forest-canopy and building heights, leaving the terrain surface that
matters for flood routing, hydrological modelling, and viewshed work.

FABDEM was produced by Laurence Hawker and Jeffrey Neal (University of Bristol /
[Fathom](https://www.fathom.global/)). This page orients the backend; for the
hands-on walkthrough see [Usage](usage.md), the dataset id on the
[Available datasets](datasets.md) page, and the rendered API on the
[Reference](fabdem.md) page.

## How it works

FABDEM V1-2 is published on the University of Bristol data repository as
**10°×10° bundle zips**, each holding one Cloud-Optimized GeoTIFF per 1°×1° land
cell. A request maps its bounding box to the intersecting bundle(s), downloads
them over anonymous HTTPS, extracts **only** the intersecting 1° tiles, then uses
the [pyramids](https://github.com/serapeum-org/pyramids) GIS backend to mosaic
the tiles and crop to the AOI — writing one GeoTIFF. `download()` returns the
written path (`list[Path]`). earthlens never imports a competing array stack; the
raster work goes through pyramids only.

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="fabdem",
    lat_lim=[50.4, 50.6],
    lon_lim=[0.4, 0.6],
    path="fabdem_out",
).download()
# -> [Path('fabdem_out/fabdem_V1-2.tif')]
```

## Two things that shape the backend

- **The DEM is static and single-band.** FABDEM is one global bare-earth grid
  (band `elevation`, metres), not a time series and with no variable to select,
  so the backend is *facet-only*: build it from just the bbox — there is no
  `variables=` / `dataset=` axis. Because there is no time axis, the
  facade-forwarded `aggregate=` is rejected (there is nothing to reduce).

- **Bundles are large; ocean is absent.** A 10° bundle is 0.8–2.4 GB, so a wide
  AOI pulls several gigabytes — keep the box tight. Ocean-only bundles and cells
  are simply not published upstream; the backend skips them, and an AOI that
  intersects no land tile raises a clear `ValueError` rather than writing an
  empty raster.

## Licence

FABDEM V1-2 is **CC-BY-NC-SA 4.0 — non-commercial use only**. `download()` emits
a `LicenseWarning` to flag the obligation. For commercial use, obtain a licence
from [Fathom](https://www.fathom.global/). Cite: Hawker, L., Uhe, P., Paulo, L.,
Sosa, J., Savage, J., Sampson, C., Neal, J. (2022). *A 30 m global map of
elevation with forests and buildings removed.* Environmental Research Letters,
17, 024016.
