# ERDDAP — usage

## Install

The backend needs the optional `erddapy` SDK:

```bash
pip install earthlens[erddap]
```

`import earthlens` works without it — `erddapy` is imported lazily, only when a tabledap dataset is downloaded.

## Pick a dataset

Every request names one curated dataset with `dataset=<id>` (see [Available datasets](datasets.md) for the
shipped ids). The id's `protocol` decides the output shape, so you do not choose griddap vs tabledap yourself —
the catalog does. An unknown id raises a `ValueError` naming the closest match.

## griddap → raster NetCDF

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="erddap",
    dataset="NOAA_DHW",                 # NOAA Coral Reef Watch, 5 km daily
    variables=["CRW_SSTANOMALY", "CRW_DHW"],
    start="2023-06-01",
    end="2023-06-03",
    lat_lim=[0.0, 5.0],                 # [south, north]
    lon_lim=[150.0, 155.0],             # [west, east]
    path="erddap_out",
).download()
# -> [PosixPath('erddap_out/NOAA_DHW.nc')]
```

Omit `variables=` to fall back to the catalog row's default set.

### Aggregating griddap output

Because griddap output is raster, you can pass an `aggregate=` config; each NetCDF is reduced per time-window into
GeoTIFFs through the pyramids flow (the same one the ECMWF backend uses):

```python
from earthlens.aggregate import AggregationConfig

paths = EarthLens(
    data_source="erddap",
    dataset="NOAA_DHW",
    variables=["CRW_SSTANOMALY"],
    start="2023-06-01",
    end="2023-06-30",
    lat_lim=[0.0, 5.0],
    lon_lim=[150.0, 155.0],
    path="erddap_out",
).download(aggregate=AggregationConfig(freq="1MS", op="mean"))
# -> [PosixPath('erddap_out/aggregated/CRW_SSTANOMALY_1MS_20230601.tif')]
```

Under `op="auto"` the reducer is picked per variable: an instantaneous *state* field (SST anomaly, DHW,
chlorophyll — all the shipped griddap datasets) reduces by `"mean"`, while a variable listed in its catalog row's
`flux_variables` (an accumulation / flux) reduces by `"sum"` (the window total). Pass an explicit `op=` to override.

## tabledap → DataFrame

```python
df = EarthLens(
    data_source="erddap",
    dataset="cwwcNDBCMet",              # NDBC standard meteorological buoys
    variables=["station", "time", "WTMP"],
    start="2023-01-01",
    end="2023-01-02",
    lat_lim=[36.0, 37.0],
    lon_lim=[-123.0, -122.0],
    path="erddap_out",
).download()
# -> pandas.DataFrame, also written to erddap_out/cwwcNDBCMet.csv
```

Pass `output_format="parquet"` to write Parquet instead of CSV. A query that matches no rows returns an **empty**
frame with the requested columns (plus a warning), rather than raising.

`aggregate=` is **not** accepted for a tabledap dataset — the facade raises `NotImplementedError`, since a table
has no gridded reduction. Use a griddap dataset (or the CMEMS backend) for gridded fields you want to reduce.

## The request → constraints mapping

The bbox (`lat_lim` / `lon_lim`) and window (`start` / `end`) become ERDDAP subset constraints:
`time>=`/`time<=` (ISO-8601, UTC) and `latitude`/`longitude` `>=`/`<=`. griddap additionally strides each axis at
full resolution. You do not build these by hand — the backend derives them from the request.
