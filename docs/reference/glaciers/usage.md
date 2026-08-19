# Glaciers — usage

The `glaciers` backend is reached through the `EarthLens` facade with
`data_source="glaciers"` (or the source aliases `"rgi"` / `"glims"` / `"wgms"`).
Pick exactly one dataset id in `variables=` — the output shape is per instance,
so one instance serves one dataset.

## RGI outlines — vector, by bounding box

Pass a bounding box (`lat_lim` / `lon_lim`, or `aoi=`). The backend maps it to
the overlapping GTN-G region(s), downloads + caches each region zip, reads it via
pyramids, and clips to the bbox. The result is a `FeatureCollection` in
EPSG:4326.

```python
from earthlens.core import EarthLens

fc = EarthLens(
    data_source="glaciers",
    variables=["rgi:outlines"],
    lat_lim=[45.8, 46.0],
    lon_lim=[6.8, 7.1],          # a small bbox over the French Alps
).download()

print(type(fc).__name__, len(fc), fc.crs)   # FeatureCollection, n outlines, EPSG:4326
print(fc[["rgi_id", "glac_name", "area_km2"]].head())
```

To pull a whole region without a bbox clip, pass a `region=` override (a GTN-G
region id, or a list of ids):

```python
fc = EarthLens(
    data_source="rgi",
    variables=["rgi:outlines"],
    region="11",                 # Central Europe; "10" spans the antimeridian
).download()
```

An RGI request needs **either** a bbox **or** a `region=` — the backend refuses
to download every region.

## GLIMS outlines — vector, time series, by bounding box

GLIMS is queried by bbox through the GLIMS WFS and returns the (possibly
multi-temporal) outlines intersecting it. Cap the number of features with
`max_features=`.

```python
fc = EarthLens(
    data_source="glims",
    variables=["glims:outlines"],
    lat_lim=[45.8, 46.0],
    lon_lim=[6.8, 7.1],
    max_features=500,
).download()
```

A GLIMS request needs a bbox (a global WFS query is too large).

## WGMS fluctuations — tabular

WGMS datasets return a `pandas.DataFrame`. The backend downloads + caches the FoG
archive once and reads the chosen table. Narrow the table with `glacier_id=`,
`glacier_name=` (a case-insensitive substring), `region=` (a GTN-G region id), or
a bounding box.

```python
df = EarthLens(
    data_source="wgms",
    variables=["wgms:mass_balance"],
    region="11",                 # glaciers in GTN-G region 11
).download()

print(df[["glacier_id", "glacier_name", "year", "annual_balance"]].head())
```

```python
# a single glacier's front-variation (length-change) record
df = EarthLens(
    data_source="wgms",
    variables=["wgms:front_variation"],
    glacier_name="aletsch",
).download()
```

The frame is also written to the output directory as CSV (or Parquet with
`output_format="parquet"`).

## Why `aggregate=` is rejected

Glacier outlines and fluctuations are pre-computed inventories / measurements,
not gridded fields, so there is no meaningful time-window or spatial reduction.
Passing a non-`None` `aggregate=` raises `NotImplementedError`.

## The download cache

RGI region zips, the GLIMS query GeoJSON, and the WGMS FoG archive are cached
under `glaciers/` in the shared earthlens cache directory so repeat requests
over the same region / archive
do not re-download. Point `path=` at a stable directory to keep the cache across
runs.
