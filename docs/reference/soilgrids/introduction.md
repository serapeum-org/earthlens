# SoilGrids — introduction

earthlens ships a single `soilgrids` backend that fetches **global soil-property
maps** from [ISRIC SoilGrids 2.0](https://www.isric.org/explore/soilgrids) —
250 m machine-learning predictions of clay, sand, silt, coarse fragments, pH,
cation exchange capacity, nitrogen and soil organic carbon — subset to a
requested bounding box and written as GeoTIFF. The data is free and keyless
(CC-BY 4.0).

SoilGrids publishes each property as an independent **OGC Web Coverage Service
(WCS)** at [`maps.isric.org`](https://maps.isric.org). Every property is served
across the six standard depth intervals × four statistical layers plus an
uncertainty layer, so a request is a `(property, depth, quantile)` triple that
maps to one coverage. This page orients the backend. For the hands-on
walkthrough see [Usage](usage.md) and the property / depth / quantile ids on the
[Available properties](datasets.md) page; the rendered API is the
[Reference](soilgrids.md) page.

## How it works — server-side WCS subset

The backend expands your request into `(property, depth, quantile)` coverage
triples and fetches each one **server-side over WCS** through the
[pyramids](https://github.com/serapeum-org/pyramids) GIS backend
(`Dataset.from_wcs`, released in pyramids 0.38.0). SoilGrids' native grid is a
custom Interrupted Goode Homolosine projection (`EPSG:152160`) that the PROJ
database does not know, so the reader is handed the IGH proj4 string as a
`coverage_crs` shim and reprojects the result to EPSG:4326 (lon/lat) by default.
Only the bbox window is returned — the full 250 m global grid is never
downloaded.

`download()` writes one cropped **GeoTIFF per requested `(property, depth,
quantile)`** and returns their paths (`list[Path]`), named
`<property>_<depth>_<quantile>.tif`. All raster I/O goes through pyramids —
earthlens never imports an OGC-WCS SDK or a competing array stack.

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="soilgrids",
    variables=["clay", "phh2o"],
    lat_lim=[51.0, 52.0],
    lon_lim=[5.0, 6.0],
    path="soil_out",
).download()
# -> 12 GeoTIFFs: 2 properties x 6 standard depths x the `mean` layer
```

## Three things that shape the backend

- **Values are scaled integers.** SoilGrids stores every property as an integer
  that must be divided by a per-property factor to reach the conventional unit
  (e.g. pH is ×10, so a stored `65` is pH 6.5; nitrogen is ×100). The backend
  **records** the unit + scale in its log but does **not** silently rescale the
  pixels — the written GeoTIFF holds the raw scaled integers, exactly as ISRIC
  serves them. See [Available properties](datasets.md) for the factors.

- **The maps are static — there is no time axis.** Each property is a single
  prediction, not a time series, so `download()` has nothing to reduce over
  time. The `EarthLens` facade forwards `aggregate=` to raster backends, so this
  backend explicitly rejects a non-`None` `aggregate=` with
  `NotImplementedError`.

- **A request is a bbox subset.** The full property grids are global; this
  backend returns only the bbox you ask for, subset server-side by the WCS.

## Attribution

SoilGrids is CC-BY 4.0; cite it when you use the data (the backend also logs the
attribution once per download):

> SoilGrids 2.0 © ISRIC — World Soil Information, licensed CC-BY 4.0; cite
> Poggio et al. (2021), *SOIL* 7, 217–240. <https://soilgrids.org>
