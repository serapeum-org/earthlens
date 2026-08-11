# DWD RADKLIM / RADOLAN — usage

The backend is reached through the `EarthLens` facade with the `"radklim"` key
(or its `"radolan"` alias) and a `dataset=` product. It needs no credentials.

## RADKLIM reprocessing (yearly NetCDF archives)

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="radklim",
    dataset="radklim-yw",          # 5-min reprocessed climatology
    start="2021-01-01",
    end="2021-12-31",
    lat_lim=[47.0, 55.0],           # must overlap Germany
    lon_lim=[6.0, 15.0],
    path="./radklim",
)
paths = lens.download()             # -> [Path('.../YW2017.002_2021_netcdf.tar.gz')]
```

`radklim-yw` is 5-min and `radklim-rw` is hourly. A `[start, end]` window maps
to whole **years**: the reprocessing is served as one `.tar.gz` NetCDF archive
per year, so a window spanning a year boundary downloads both years'
archives. These are large files (YW ~13.5 GB/yr, RW ~836 MB/yr).

## Operational RADOLAN (per-timestamp granules)

```python
lens = EarthLens(
    data_source="radklim",
    dataset="radolan-yw",           # 5-min operational, near-real-time
    start="2026-08-10T00:00",
    end="2026-08-10T06:00",
    fmt="%Y-%m-%dT%H:%M",
    lat_lim=[47.0, 55.0],
    lon_lim=[6.0, 15.0],
    path="./radolan",
)
paths = lens.download()             # per-5-min .hdf5 granules in the window
```

The operational stream keeps only a rolling **~2-day** window. A request older
than that returns an empty list and logs a warning pointing you at the RADKLIM
archive.

To fetch the RADOLAN **binary** instead of HDF5 (needs a `wradlib`/pyramids
decoder downstream), pass `data_format="bin"`:

```python
EarthLens(data_source="radklim", dataset="radolan-rw", data_format="bin", ...)
```

## Reading a downloaded granule (pyramids)

earthlens returns raw granule paths; read them with pyramids. The operational
HDF5 opens directly:

```python
from pyramids.dataset import Dataset

ds = Dataset.read_file(str(paths[0]))   # operational .hdf5
print(ds.shape)   # grid-dependent — e.g. (1, 1200, 1100) for the RW HDF5 above
```

The RADKLIM `.tar.gz` is a gzip-of-tar of per-timestamp `.nc` files; extract it
(stdlib `tarfile`) and read the member `.nc` with pyramids.

## Attribution

RADKLIM / RADOLAN are DWD open geodata under CC-BY-4.0 / GeoNutzV. Credit
**Deutscher Wetterdienst (DWD)** wherever you publish results derived from them.
