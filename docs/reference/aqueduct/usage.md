# WRI Aqueduct riverine flood risk — usage

The `aqueduct` backend needs **no credentials** — the 2015 Analyzer data is
public on `files.wri.org`. A request selects one admin level, metric, year, and
scenario across the requested flood return periods; the downloaded shapefile is
cached, so repeated requests for the same admin level reuse it.

For the concepts (what the data is, what is available, the licence) see
[Introduction](introduction.md); the rendered API is the [Reference](aqueduct.md)
page. A runnable walkthrough is the
[quickstart notebook](../../examples/aqueduct/quickstart.ipynb).

## Quick start

```python
from earthlens.core import EarthLens

# Country-level population exposed to river flooding, 2010 baseline,
# the 100-year flood.
fc = EarthLens(
    "aqueduct",
    admin_level="country",
    metric="population_affected",
    year=2010,
    scenario="baseline",
    return_period=100,
    path="aqueduct-data",
).download()

fc.head()  # a FeatureCollection: unit_id, unit_name, rp_100, geometry
```

`download()` returns a pyramids `FeatureCollection` (a `geopandas.GeoDataFrame`
subclass) and also writes it to a GeoPackage under `path`.

## Selecting the risk dimension

```python
# GDP exposed under a 2030 projection (SSP2 + RCP8.5), several return periods.
fc = EarthLens(
    "aqueduct",
    admin_level="basin",
    metric="gdp_affected",
    year=2030,
    scenario="ssp2-rcp8p5",
    return_period=[100, 250, 1000],
).download()
# columns: unit_id, unit_name, rp_100, rp_250, rp_1000, geometry
```

- Omit `return_period` to get all nine flood magnitudes (`rp_2` … `rp_1000`).
- `metric` is one of `gdp_affected`, `population_affected`, `urban_damage`.
- A 2030 `scenario` is invalid with `year=2010` (and vice versa) — the 2010
  baseline uses `scenario="baseline"`.

## Filtering by area

```python
# A single country by name (case-insensitive), at country level.
kenya = EarthLens(
    "aqueduct", admin_level="country", country="Kenya", return_period=100
).download()

# Any admin level, narrowed to a bounding box.
horn = EarthLens(
    "aqueduct",
    admin_level="basin",
    lat_lim=[-5, 15],
    lon_lim=[32, 52],
    return_period=100,
).download()
```

At country level `country=` matches the country name. Below country level
`country=` matches the unit's own name (the state layer carries an unused `admin`
country column, the basin layer none), so use `lat_lim` / `lon_lim` to narrow a
region.

## Table output (no geometry)

```python
df = EarthLens(
    "aqueduct",
    admin_level="country",
    metric="urban_damage",
    return_period=100,
    geometry=False,
).download()  # a pandas.DataFrame: unit_id, unit_name, rp_100
```

## Notes

- **`aggregate=` is rejected** — the data is admin-aggregated exposure, not a
  gridded raster.
- The values are per-return-period exposure, not a single expected-annual figure.
- Coastal flooding and the 2050 / 2080 horizons are the paywalled 2020 product
  and are not available here; `hazard="coastal"` raises.
