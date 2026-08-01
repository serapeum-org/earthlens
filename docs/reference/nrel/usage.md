# NREL — usage

All examples go through the [`EarthLens`](../earthlens.md) facade with
`data_source="nrel"` (aliases `"nsrdb"` / `"wind-toolkit"`). The backend needs
a **free API key + email** (see [Introduction](introduction.md#authentication-is-required)).
`download()` returns a long-format `pandas.DataFrame` and also writes it to
`path` as CSV (or Parquet — see below).

## Credentials

Pass them explicitly, or set them once in the environment:

```python
df = EarthLens(
    data_source="nrel",
    variables=["ghi", "dni"],
    point=(39.74, -105.18),
    start="2020-01-01",
    end="2020-12-31",
    api_key="YOUR_KEY",
    email="you@example.com",
).download()
```

```bash
export NREL_API_KEY="YOUR_KEY"
export NREL_EMAIL="you@example.com"
```

A missing key raises an `AuthenticationError` naming `NREL_API_KEY`; a key with
no email raises one naming `NREL_EMAIL`.

## Pick a product with `product=` (or an alias)

`variables=` lists the **attributes** to pull; `product=` picks the dataset:

| `product=` | alias | What you get |
|------------|-------|--------------|
| `"nsrdb-psm3"` (default) | `data_source="nsrdb"` | NSRDB GOES v4 hourly solar, one year per call |
| `"nsrdb-tmy"` | — | NSRDB GOES TMY v4 typical-meteorological-year |
| `"wtk"` | `data_source="wind-toolkit"` | WIND Toolkit hourly wind |

```python
from earthlens.core import EarthLens

# Solar (NSRDB) — the default product
solar = EarthLens(
    data_source="nrel",
    variables=["ghi", "dni", "dhi", "air_temperature"],
    start="2020-01-01",
    end="2020-12-31",
    point=(39.74, -105.18),
    path="nrel_out",
).download()
# solar.columns -> time, GHI, DNI, DHI, Temperature, lat, lon, year, product

# Wind (WIND Toolkit) — via the alias
wind = EarthLens(
    data_source="wind-toolkit",
    variables=["windspeed_100m", "winddirection_100m"],
    start="2012-01-01",
    end="2012-12-31",
    point=(39.74, -105.18),
    path="wtk_out",
).download()
```

`start` / `end` set the year window: a year-based product issues **one call per
calendar year** in the window; the `nsrdb-tmy` product ignores the window and
makes a single `names=tmy` call. An empty `variables=` falls back to the
product's default attribute list.

## Single point vs. a bbox grid

- **A single point** — pass `point=(lat, lon)`. It wins over any
  `lat_lim` / `lon_lim` (including the whole-Earth defaults the facade injects),
  so the request is exactly that one coordinate.
- **A bounding box** — pass `lat_lim` / `lon_lim` (or `aoi=`). The box is
  sampled to a `(lat, lon)` grid at `spacing_deg` (default `0.5°`); each grid
  point × each year is one request.

```python
grid = EarthLens(
    data_source="nrel",
    variables=["ghi"],
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[39.0, 40.0],
    lon_lim=[-106.0, -105.0],
    spacing_deg=0.5,         # 3x3 = 9 points x 1 year -> 9 GETs
    path="nrel_grid",
).download()
```

!!! warning "Mind the request count + rate limit"
    Each `(point, year)` is one keyed CSV call, throttled to **≤ 1 req/s** with a
    **5000 req/day** cap. The backend warns past a soft threshold and raises a
    `ValueError` past `max_requests` (default 500). Coarsen `spacing_deg`, shrink
    the box, narrow the years, or raise `max_requests=` deliberately for a large
    grid — and remember a big fan-out takes at least one second per call.

## Interval and timestamps

`interval=` selects the resolution in minutes (`60` default, or `30` where the
product offers it); `utc="true"` returns UTC timestamps instead of local time.

## Output format

`output_format="csv"` (default) or `"parquet"` selects the on-disk table; the
Parquet path needs `pyarrow`. The written file is `nrel_<product>.<ext>` under
`path`.

## Why `aggregate=` is rejected

NREL returns an already-resolved hourly / TMY series, so there is no gridded
reduction to apply. The facade rejects a non-`None` `aggregate=` for this
`tabular` backend with `NotImplementedError`. Resample the returned `DataFrame`
in pandas (`df.set_index("time").resample("1D").mean()`) for a coarser cadence.

## Coverage caveat

NREL coverage is region-dependent (NSRDB: Americas + parts of Asia / Africa /
Europe; WTK: CONUS + offshore). A single out-of-coverage `point=` raises a
`ValueError` naming the coordinate; an out-of-coverage point inside a bbox grid
is skipped with a warning while the in-coverage points are kept.
