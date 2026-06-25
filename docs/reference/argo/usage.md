# Argo float profiles — usage

The `argo` backend needs the `argopy` SDK:

```bash
pip install earthlens[argo]
```

`argopy` pins a newer `xarray` than some other backends (notably
`openeo`), so it is intentionally **not** part of the `[all]` extra —
install `[argo]` into its own environment.

All examples return a long-format `pandas.DataFrame` (one row per
measured level); there are no credentials.

## Region selection (bbox + time)

Name the parameters you want; the bbox + time window + depth range build
the `argopy` region box:

```python
from earthlens import EarthLens

df = EarthLens(
    "argo",
    variables=["TEMP", "PSAL"],
    start="2020-01-01",
    end="2020-01-31",
    lat_lim=[40.0, 45.0],
    lon_lim=[-60.0, -55.0],
).download()

df[["PLATFORM_NUMBER", "CYCLE_NUMBER", "TIME", "PRES", "TEMP", "PSAL"]].head()
```

The bbox can also be given as a single `aoi=` (a `(west, south, east,
north)` tuple, a shapely / GeoJSON geometry, or a `GeoDataFrame`). Tune
the depth envelope with `depth=(0, 1000)`.

## A specific float, or one profile

A `float:` / `profile:` selector targets a float by its WMO id; the bbox
is then ignored:

```python
# Every profile from one float
EarthLens("argo", variables=["float:6902746"],
          start="2020-01-01", end="2020-12-31").download()

# Several floats at once
EarthLens("argo", variables=["float:6902746,6902747"],
          start="2020-01-01", end="2020-12-31").download()

# One float's cycle 12
EarthLens("argo", variables=["profile:6902746/12"],
          start="2020-01-01", end="2020-12-31").download()
```

## Biogeochemical floats

Switch the family with `dataset="bgc"` and name BGC parameters
(validated against the catalog, with a did-you-mean hint on a typo):

```python
EarthLens(
    "argo",
    variables=["DOXY", "CHLA"],
    dataset="bgc",
    start="2021-03-01",
    end="2021-03-31",
    lat_lim=[-65.0, -55.0],
    lon_lim=[-30.0, -10.0],
).download()
```

## Transport, QC mode, and output format

```python
EarthLens(
    "argo",
    variables=["TEMP", "PSAL"],
    source="gdac",        # "erddap" (default) / "gdac" / "argovis"
    mode="expert",        # "standard" (default) / "expert" / "research"
    output_format="parquet",  # "csv" (default) / "parquet"
    start="2020-01-01", end="2020-01-31",
    lat_lim=[40.0, 45.0], lon_lim=[-60.0, -55.0],
).download()
```

The `download()` call also writes the table to the output directory
(`argo_<family>_<selection>.csv` / `.parquet`).

## What `aggregate=` does (nothing)

Argo profiles are irregular point data, so there is no gridded reduction.
Passing `aggregate=` raises `NotImplementedError`; for gridded ocean
fields use the [CMEMS](../cmems/introduction.md) backend instead.
