# CMIP6 — usage

## Download a bbox/time subset

Pin a facet tuple, a date window, and a bounding box; `download()` returns the
`list[Path]` of written NetCDF subsets (one per resolved store):

```python
from earthlens.core import EarthLens

paths = EarthLens(
    "cmip6",
    source_id="CanESM5",        # the model
    experiment_id="ssp585",     # the scenario
    variable_id="tas",          # near-surface air temperature
    table_id="Amon",            # monthly atmosphere
    member_id="r1i1p1f1",       # variant (the default)
    start="2050-01-01",
    end="2050-12-31",
    lat_lim=[35.0, 60.0],       # crop the native grid to Europe
    lon_lim=[-10.0, 30.0],
    path="cmip6-out",
).download()
```

Everything is anonymous — no credentials are needed.

## The facet tuple

`source_id`, `experiment_id`, `variable_id`, and `table_id` are required; the
rest have sensible defaults:

- `member_id` defaults to `r1i1p1f1` (present for nearly every model).
- `version` defaults to `"latest"` (the newest publication per store).
- `grid_label` is unpinned by default; if a model publishes more than one grid
  for the request, the download **fans out** to one NetCDF per grid.

An unknown facet raises a `ValueError` that names the offending facet and lists
the values that were available:

```python
EarthLens(
    "cmip6", source_id="CanESM5", experiment_id="ssp999",  # typo
    variable_id="tas", table_id="Amon", start="2050-01-01", end="2050-12-31",
).download()
# ValueError: no CMIP6 store matches experiment_id='ssp999' ... Available experiment_id: [...]
```

## Whole-grid and whole-series reads

Both are opt-in and warned (the stores are large):

```python
# whole native grid (no bbox) — leave lat_lim / lon_lim at whole-Earth
EarthLens("cmip6", source_id="CanESM5", experiment_id="ssp585",
          variable_id="tas", table_id="Amon",
          start="2050-01-01", end="2050-12-31").download()

# whole time series (skip the date-window subset)
EarthLens("cmip6", source_id="CanESM5", experiment_id="ssp585",
          variable_id="tas", table_id="Amon", whole_time=True,
          start="2015-01-01", end="2100-12-31",
          lat_lim=[35, 60], lon_lim=[-10, 30]).download()
```

## Inspect the catalog

The bundled catalog carries the config plus a curated vocabulary of the common
variables / experiments / tables / sources (resolution itself runs against the
full CSV, so uncurated facets still download):

```python
from earthlens.cmip6 import Catalog

cat = Catalog()
cat.get_dataset("tas").long_name        # 'Near-surface (2 m) air temperature'
cat.get_experiment("ssp585").activity_id  # 'ScenarioMIP'
cat.get_table("Amon").cadence           # 'monthly'
cat.terms_note("CanESM5")               # per-model attribution note
```

## Attribution

Aggregation (`aggregate=`) is not wired for CMIP6 — the written NetCDFs can be
aggregated separately with `earthlens.aggregate.aggregate_netcdf`. Always cite
the source GCM; `CMIP6(...).terms_note()` returns the per-model note.
