# SoilGrids — usage

The `soilgrids` backend downloads ISRIC SoilGrids 2.0 soil-property maps for a
bounding box and writes one GeoTIFF per `(property, depth, quantile)`. It needs
no credentials (CC-BY 4.0). See [Available properties](datasets.md) for the
`variables=` / `depths=` / `quantiles=` ids and [Introduction](introduction.md)
for how the WCS transport and scaled-integer units work.

## A two-property subset

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="soilgrids",
    variables=["clay", "phh2o"],
    lat_lim=[51.0, 52.0],     # [south, north]
    lon_lim=[5.0, 6.0],       # [west, east], -180..180
    path="soil_out",
).download()

len(paths)   # 12 — 2 properties x 6 standard depths x the `mean` layer
paths[0]     # Path('soil_out/clay_0-5cm_mean.tif')
```

`download()` returns the list of written GeoTIFF paths, one per requested
`(property, depth, quantile)`, named `<property>_<depth>_<quantile>.tif` under
`path`.

## Choosing depths and quantiles

By default a request fetches **every depth the property publishes at the `mean`
layer**. Narrow it with `depths=` and `quantiles=`:

```python
EarthLens(
    data_source="soilgrids",
    variables=["phh2o"],
    lat_lim=[51.0, 52.0],
    lon_lim=[5.0, 6.0],
    path="soil_out",
    depths=["0-5cm", "5-15cm"],       # topsoil only
    quantiles=["Q0.05", "mean", "Q0.95"],  # add the 5th/95th percentiles
).download()
# -> 6 GeoTIFFs: phh2o x {0-5cm, 5-15cm} x {Q0.05, mean, Q0.95}
```

The standard depths are `0-5cm, 5-15cm, 15-30cm, 30-60cm, 60-100cm, 100-200cm`;
the layers are `Q0.05, Q0.5, Q0.95, mean, uncertainty`. The `ocs` (organic
carbon stock) property is the exception — it publishes a single `0-30cm` depth.
An unknown property, depth or quantile raises a `ValueError` with a did-you-mean
hint. List the curated properties with
`earthlens.soilgrids.Catalog().parameters()`.

## Reading a result — the scaled-integer units

SoilGrids stores each property as a **scaled integer**; divide by the
property's factor to get the conventional unit. The backend keeps the raw
integers in the GeoTIFF (it does not rescale), so apply the factor yourself:

```python
from pyramids.dataset import Dataset

ph = Dataset.read_file("soil_out/phh2o_0-5cm_mean.tif")
ph.epsg                              # 4326 (WGS84, the default output CRS)
scaled = ph.read_array()             # e.g. 65  (pH x10)
real_ph = scaled / 10.0              # -> 6.5   (phh2o scale factor is 10)
```

The no-data sentinel is `-32768`. The per-property scale factors are on the
[Available properties](datasets.md) page (and in each `Catalog` row's
`scale_factor`).

## Output CRS and resolution

By default the result is reprojected to **EPSG:4326** (lon/lat). Keep the
native Interrupted Goode Homolosine grid with `output_crs=None`, or reproject
anywhere with `output_crs="EPSG:3857"`; set the pixel size in output-CRS units
with `resolution=` (defaults to the native 250 m):

```python
EarthLens(
    data_source="isric",                 # `isric` is an alias for `soilgrids`
    variables=["soc"],
    lat_lim=[51.0, 52.0],
    lon_lim=[5.0, 6.0],
    path="soil_out",
    output_crs=None,                     # native IGH equal-area grid
).download()
```

## No temporal aggregation

SoilGrids is a single static prediction with no time axis, so there is nothing
to reduce over time. Passing `aggregate=` raises `NotImplementedError`:

```python
from earthlens.aggregate import AggregationConfig

EarthLens(
    data_source="soilgrids",
    variables=["clay"],
    lat_lim=[51.0, 52.0],
    lon_lim=[5.0, 6.0],
).download(aggregate=AggregationConfig(freq="YS", op="mean"))
# NotImplementedError: ... static soil-property map with no temporal axis ...
```
