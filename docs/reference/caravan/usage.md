# Caravan — usage

`earthlens.caravan` returns a long `pandas.DataFrame`: one row per catchment-day, columns
`gauge_id`, `date`, then the variables you asked for. `OUTPUT_KIND` is `"tabular"`, so the facade rejects an
`aggregate=` argument — reduce the frame with pandas instead.

No credentials are needed anywhere on this page.

## Selecting catchments

A request **must** narrow the catchments somehow. An unbounded request raises rather than pulling every catchment
in the extension. There are three ways to narrow it, and they compose.

### By explicit id

```python
from earthlens.core import EarthLens

flow = EarthLens(
    "caravan",
    dataset="grdc",
    variables=["streamflow"],
    start="2000-01-01", end="2000-12-31",
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    gauge_ids=["GRDC_1159100", "GRDC_1159103"],
).download()
```

Ids carry their source as a prefix, and **the casing is not uniform**: `GRDC_1159100` but `camelsdk_100006`,
`camelsde_DE110000`, `il_12130`, `hysets_01010070`. An unknown id raises and shows real ids from the archive.

### By bounding box

```python
flow = EarthLens(
    "caravan",
    dataset="israel",
    variables=["streamflow", "total_precipitation"],
    start="2020-01-01", end="2020-12-31",
    lat_lim=[32.0, 33.0], lon_lim=[34.5, 35.5],
).download()
```

The box is matched against each catchment's gauge coordinates from the archive's own
`attributes_other_<source>.csv`.

### By country

```python
flow = EarthLens(
    "caravan",
    dataset="denmark",
    variables=["streamflow"],
    start="2019-01-01", end="2019-12-31",
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    country="Denmark",
).download(limit=500)
```

!!! note "`country=` matches the full English name"
    The archive stores `country` as `"Denmark"` / `"South Africa"`, not an ISO2 code. Matching is
    case-insensitive, so `country="denmark"` works, but `country="DK"` does not.

## Variables

Pass friendly names or the archive's own column names — both resolve:

| friendly name | archive column | units |
|---|---|---|
| `streamflow` | `streamflow` | mm/day |
| `total_precipitation` | `total_precipitation_sum` | mm/day |
| `potential_evaporation` | `potential_evaporation_sum_ERA5_LAND` | mm/day |
| `potential_evaporation_fao` | `potential_evaporation_sum_FAO_PENMAN_MONTEITH` | mm/day |
| `temperature_2m_mean` | `temperature_2m_mean` | °C |

Forcing variables come as `{min,mean,max}` triples for temperature, dewpoint, snow water equivalent, net solar
and thermal radiation, surface pressure, both wind components, and four soil-moisture layers. List them all with
`Catalog().variables`.

!!! warning "`streamflow` is mm/day, and blanks are real"
    Discharge is normalised by catchment area, so it is a depth per day, not m³/s. Gaps in the record come back
    as `NaN` — these are genuine missing observations and are deliberately **not** dropped.

## Capping the result

```python
flow = EarthLens("caravan", dataset="denmark", country="Denmark", ...).download(limit=1000)
```

For a ZIP archive the cap genuinely **stops the work** — catchments past it are never read. See
[bounded results](../base/bounded-results.md).

## Optional extras

```python
source = EarthLens(
    "caravan",
    dataset="denmark",
    with_attributes=True,   # join the static catchment attributes onto every row
    with_geometry=True,     # read the basin polygons
    ...,
)
frame = source.download()
basins = source.datasource.geometry   # a pyramids FeatureCollection
```

`with_attributes` adds the catchment's area, country, gauge name and the Caravan climate indices (aridity,
`p_mean`, snow fraction, seasonality).

## Why there is no NetCDF option

Each archive also publishes a `.nc` variant of the *same* data — same catchments, same columns, same period. The
backend does not read it, and `timeseries_format="netcdf"` raises `NotImplementedError`.

The reason is a genuine boundary rather than an oversight. Caravan's `.nc` members are **1-D per-catchment time
series**, but pyramids — which owns every array container in this ecosystem — models NetCDF as *raster*, so it
opens them as an empty zero-band grid. Decoding them properly would need `h5py` / `netCDF4` / `xarray`, none of
which earthlens depends on, and adding one to duplicate data the CSV path already returns is not a trade worth
making. The catalog still records the `.nc` files, because they are real and a future backend may want them.

If you want to work with the `.nc` archive directly, [Inside a GRDC-Caravan archive](archive-contents.md)
documents its variables, dtypes and global attributes — including the two things the CSV variant does **not**
carry: the unit list and the catchment's local timezone.

## The `base` extension is opt-in

`base` at its current version (1.6) is a **24.8 GB** (netCDF) / **29.0 GB** (CSV) `.tar.gz`. A gzip stream has no
directory, so reaching one catchment means transferring all of it. Requesting it raises unless you opt in:

```python
EarthLens("caravan", dataset="base", allow_full_download=True, ...)   # 29 GB, cached after the first fetch
```

The alternative is the last range-readable base release:

```python
EarthLens("caravan", dataset="base", version="1.2", gauge_ids=["camels_01022500"], ...)   # ~3 MB
```

!!! danger "`version="1.2"` is not merely an older cut of the same data"
    It holds **6,830** catchments against 1.6's 16,299, its forcing **stops in 2020** rather than 2023, and it
    predates the PET split — you get one `potential_evaporation_sum` instead of the ERA5-Land/FAO pair. Use it for
    cheap exploration, not as a drop-in substitute.

## Caching and rate limits

Only the `tar.gz` path writes to disk, under `EARTHLENS_CACHE` if set, otherwise the per-platform user cache
directory. A cached archive whose md5 matches the catalog is reused, so the big download happens at most once.

Zenodo rate-limits anonymous callers, and each catchment costs two ranged reads, so the client throttles to one
request per second by default. Override with `min_interval=`. A selection of more than 25 catchments warns up
front with a time estimate.

## Checking for new releases

```bash
earthlens datasets refresh caravan
```

Reports any extension that has gained a release newer than its pin, and any Caravan record on Zenodo the catalog
does not track. It never rewrites the catalog — bumping a pin means re-checking the archive layout and column
set, which is a human decision.
