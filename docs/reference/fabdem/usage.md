# FABDEM — usage

The `fabdem` backend downloads a FABDEM V1-2 bare-earth DEM subset for a bounding
box and writes it as a GeoTIFF. It needs no credentials (FABDEM is open, though
non-commercial). See [Introduction](introduction.md) for how the transport works
and [Available datasets](datasets.md) for the product details.

## A FABDEM subset

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="fabdem",
    lat_lim=[50.4, 50.6],     # [south, north]
    lon_lim=[0.4, 0.6],       # [west, east], -180..180
    path="fabdem_out",
).download()

paths           # [Path('fabdem_out/fabdem_V1-2.tif')]
```

`download()` returns the list of written GeoTIFF paths (one mosaicked, cropped
DEM). The downloaded bundle zips are cached under `fabdem_out/.fabdem_cache/`
during the run and the bundle is removed once its tiles are extracted.

Read the result back with pyramids:

```python
from pyramids.dataset import Dataset

dem = Dataset.read_file("fabdem_out/fabdem_V1-2.tif")
dem.epsg                 # 4326 (WGS84)
array = dem.read_array() # bare-earth elevation in metres
```

The `fab-dem` and `bare-earth-dem` aliases route to the same backend.

## The non-commercial licence warning

FABDEM is CC-BY-NC-SA 4.0, so every `download()` emits a `LicenseWarning`:

```python
import warnings
from earthlens.biodiversity import LicenseWarning

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    EarthLens(
        data_source="fabdem",
        lat_lim=[50.4, 50.6],
        lon_lim=[0.4, 0.6],
        path="fabdem_out",
    ).download()

assert any(isinstance(w.message, LicenseWarning) for w in caught)
```

For commercial use, obtain a licence from [Fathom](https://www.fathom.global/).

## Keep the bounding box tight

FABDEM ships as 10°×10° bundle zips of 0.8–2.4 GB each, so a wide AOI downloads
several gigabytes. Request the smallest box you need. An AOI that intersects only
ocean (no published land tile) raises a clear `ValueError`:

```python
EarthLens(
    data_source="fabdem",
    lat_lim=[-41.0, -40.8],
    lon_lim=[-140.2, -140.0],   # open South Pacific -> no land tiles
    path="fabdem_out",
).download()
# ValueError: ... intersects no published 1 degree tile (ocean-only area) ...
```

## No temporal aggregation

FABDEM is a single static grid, so passing `aggregate=` is rejected — there is
no time axis to reduce. Fetch the DEM, then combine it with time-varying layers
from another backend.
