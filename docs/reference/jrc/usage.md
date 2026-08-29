# JRC hazards — usage

The `jrc-flood` backend downloads the JRC European Flood Hazard Map (EFHM) for a
bounding box and one or more return periods, writing one GeoTIFF of water depth
(m) per return period. It needs no credentials (CC-BY-4.0). See
[Introduction](introduction.md) for the transport and
[Available datasets](datasets.md) for the return periods.

## A single return period

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="jrc-flood",
    lat_lim=[51.8, 52.0],       # [south, north]
    lon_lim=[4.8, 5.0],         # [west, east], -180..180
    return_periods=[100],       # 1-in-100-year flood
    path="efhm_out",
).download()

paths           # [Path('efhm_out/efhm_RP100.tif')]
```

Read the result back with pyramids:

```python
from pyramids.dataset import Dataset

depth = Dataset.read_file("efhm_out/efhm_RP100.tif")
depth.epsg                  # 4326 (WGS84)
array = depth.read_array()  # river-flood water depth in metres (-9999 = no data)
```

The `efhm`, `jrc-flood-hazard`, and `european-flood-hazard` aliases route to the
same backend.

## Several return periods at once

```python
EarthLens(
    data_source="jrc-flood",
    lat_lim=[51.8, 52.0],
    lon_lim=[4.8, 5.0],
    return_periods=[10, 100, 500],   # ints, "100", or "RP100" all work
    path="efhm_out",
).download()
# -> [efhm_RP10.tif, efhm_RP100.tif, efhm_RP500.tif]
```

Each return period is written to its own `efhm_RP{n}.tif`. Requesting an
unpublished return period raises a `ValueError` listing the available ones.

## Outside the coverage

The EFHM covers Europe and the Mediterranean Basin. An AOI outside that extent
raises rather than writing an empty raster:

```python
EarthLens(
    data_source="jrc-flood",
    lat_lim=[-5.0, -4.8],
    lon_lim=[30.0, 30.2],     # equatorial Africa -> outside EFHM coverage
    return_periods=[100],
    path="efhm_out",
).download()
# ValueError: ... the AOI ... is outside the EFHM's Europe / Mediterranean coverage ...
```

## Global flood hazard

For the **global** JRC flood hazard (covering Europe too, at ~90 m), use the
[`gee`](../gee/introduction.md) backend with `asset="JRC/CEMS_GLOFAS/FloodHazard/v2_1"`.
The `jrc-flood` backend here serves the separate, Europe-focused EFHM product.

## No temporal aggregation

The return-period grids are static (a return period is not a time step), so
passing `aggregate=` is rejected.

## Sea-level (Total Water Level) forecasts

The same backend serves the JRC probabilistic sea-level forecasts. Select the
gridded product with `product=`; the `coastal-forecast` key returns the global
per-country summary instead. By default the newest complete forecast cycle is
used (`reference_time="latest"`); pass an explicit cycle to pin one.

```python
# gridded medium-term TWL forecast, latest cycle, cropped to the North Sea
paths = EarthLens(
    data_source="sea-level-forecast",
    product="medium_term",           # or "subseasonal"
    lat_lim=[51.0, 53.0],
    lon_lim=[3.0, 5.0],
    path="twl_out",
).download()
# -> [Path('twl_out/sea_level_medium_term_<cycle>_TWL75.tif')]  (one band per forecast step)

# a specific cycle + a different field
EarthLens(
    data_source="sea-level-forecast",
    product="subseasonal",
    reference_time="2026-08-24T00",
    field="probabilityTWL_01_15-100",
    lat_lim=[51.0, 53.0],
    lon_lim=[3.0, 5.0],
    path="twl_out",
).download()

# subseasonal global per-country coastal summary -> a pandas.DataFrame
summary = EarthLens(data_source="coastal-forecast").download()
summary.head()
```

As with the EFHM, only the AOI window is read over `/vsicurl`, and `aggregate=`
is rejected — a forecast cycle is chosen by `reference_time`, not reduced over
time.
