# Bathymetry — usage

The `bathymetry` backend downloads a global DEM subset for a bounding box
and writes it as a GeoTIFF. It needs no credentials (both GEBCO and ETOPO1
are open). See [Available datasets](datasets.md) for the `dataset=` ids and
[Introduction](introduction.md) for how the transport works.

## A GEBCO bathymetry subset

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="bathymetry",
    dataset="gebco_2020",
    lat_lim=[25.0, 26.0],     # [south, north]
    lon_lim=[-18.0, -17.0],   # [west, east], -180..180
    path="bathy_out",
).download()

paths           # [Path('bathy_out/gebco_2020.tif')]
```

`download()` returns the list of written GeoTIFF paths. The intermediate
NetCDF (`gebco_2020.nc`) is kept alongside the GeoTIFF under `path`.

Read the result back with pyramids:

```python
from pyramids.dataset import Dataset

dem = Dataset.read_file("bathy_out/gebco_2020.tif")
dem.epsg                 # 4326 (WGS84)
array = dem.read_array() # elevation in metres; negative below sea level
```

## ETOPO1 ice surface vs bedrock

ETOPO1 ships two variants — pick the one you need with `dataset=`, or use
the `etopo` alias:

```python
# Ice surface (top of the ice sheets)
EarthLens(
    data_source="etopo",
    dataset="etopo1_ice",
    lat_lim=[-78.0, -76.0],
    lon_lim=[160.0, 170.0],
    path="etopo_out",
).download()

# Bedrock (base of the ice sheets)
EarthLens(
    data_source="etopo",
    dataset="etopo1_bedrock",
    lat_lim=[-78.0, -76.0],
    lon_lim=[160.0, 170.0],
    path="etopo_out",
).download()
```

## No temporal aggregation

A DEM is a single static grid with no time axis, so there is nothing to
reduce over time. Passing `aggregate=` raises `NotImplementedError`:

```python
from earthlens.aggregate import AggregationConfig

EarthLens(
    data_source="bathymetry",
    dataset="gebco_2020",
    lat_lim=[25.0, 26.0],
    lon_lim=[-18.0, -17.0],
).download(aggregate=AggregationConfig(freq="YS", op="mean"))
# NotImplementedError: ... a static ... DEM ... has no temporal axis ...
```

For gridded ocean fields you want to aggregate over time, use the CMEMS
backend instead.

## Keeping the bbox reasonable

The backend logs the estimated subset size before fetching and warns when
a request is very large. If the bbox is outside a DEM's coverage, or so
large the ERDDAP server rejects it, `download()` raises a `ValueError`
telling you to shrink the box:

```python
EarthLens(
    data_source="bathymetry",
    dataset="gebco_2020",
    lat_lim=[-90.0, 90.0],
    lon_lim=[-180.0, 180.0],   # the whole globe at 15" -> rejected
).download()
# ValueError: ... the bbox may be outside coverage or too large (shrink it).
```
