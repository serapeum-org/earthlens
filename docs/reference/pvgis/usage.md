# PVGIS — usage

All examples go through the [`EarthLens`](../earthlens.md) facade with
`data_source="pvgis"` (topic key `"pvgis:solar-pv"`). The backend needs no credentials.
`download()` returns a long-format `pandas.DataFrame` and also writes it to
`path` as CSV (or Parquet — see below).

## Pick a tool with `variables=`

`variables` is a **single-element list** naming the product / tool:

| `variables` | Tool | What you get |
|-------------|------|--------------|
| `["seriescalc"]` | hourly series | Radiation (+ PV power) hour-by-hour over the year window |
| `["tmy"]` | typical meteorological year | One synthetic 8760-hour year of meteo variables |

```python
from earthlens.core import EarthLens

series = EarthLens(
    data_source="pvgis",
    variables=["seriescalc"],
    start="2020-01-01",
    end="2020-12-31",
    point=(45.0, 8.0),
    path="pvgis_out",
).download()
# series.columns -> time, G(i), H_sun, T2m, WS10m, Int, lat, lon, product
```

`start` / `end` set the `seriescalc` year window (`startyear` / `endyear`); the
`tmy` tool ignores the window (it is a multi-year synthesis).

## Single point vs. a bbox grid

- **A single point** — pass `point=(lat, lon)`. It wins over any
  `lat_lim` / `lon_lim` (including the whole-Earth defaults the facade injects),
  so the request is exactly that one coordinate and one keyless GET.
- **A bounding box** — pass `lat_lim` / `lon_lim` (or `aoi=`). The box is
  sampled to a `(lat, lon)` grid at `spacing_deg` (default `0.1°`); each grid
  point is one request.

```python
grid = EarthLens(
    data_source="pvgis",
    variables=["seriescalc"],
    start="2020-01-01",
    end="2020-12-31",
    lat_lim=[45.0, 45.5],
    lon_lim=[8.0, 8.5],
    spacing_deg=0.25,        # 3x3 = 9 points -> 9 GETs
    path="pvgis_grid",
).download()
```

!!! warning "Mind the request count"
    A bbox at a fine `spacing_deg` explodes into many keyless GETs, throttled to
    **≤30 req/s**. The backend warns past a soft threshold and raises a
    `ValueError` past `max_points` (default 400). Coarsen `spacing_deg`, shrink
    the box, or raise `max_points=` deliberately if you really want a large grid.

## PV-system knobs

For `seriescalc`, switch on PV-power modelling and tune the array with keyword
arguments forwarded verbatim to the PVGIS query (they merge over the catalog
defaults):

| kwarg | PVGIS meaning |
|-------|---------------|
| `pvcalculation=1` | return PV system power `P` (W) |
| `peakpower=1` | installed peak power (kWp) |
| `loss=14` | system losses (%) |
| `angle=35` | panel tilt (°) |
| `aspect=0` | azimuth (° from south; 0 = south) |
| `raddatabase="PVGIS-SARAH3"` | radiation database (`PVGIS-SARAH3` / `PVGIS-ERA5` / `PVGIS-NSRDB`, region-dependent) |

```python
pv = EarthLens(
    data_source="pvgis",
    variables=["seriescalc"],
    start="2020-01-01",
    end="2020-12-31",
    point=(45.0, 8.0),
    pvcalculation=1,
    peakpower=1,
    loss=14,
    angle=35,
    aspect=0,
    path="pvgis_pv",
).download()
# now includes a 'P' column (PV system power, W)
```

## Output format

`output_format="csv"` (default) or `"parquet"` selects the on-disk table; the
Parquet path needs `pyarrow`. The written file is `pvgis_<tool>.<ext>` under
`path`.

## Why `aggregate=` is rejected

PVGIS returns an already-resolved hourly / TMY series, so there is no gridded
reduction to apply. The facade rejects a non-`None` `aggregate=` for this
`tabular` backend with `NotImplementedError`. Resample the returned `DataFrame`
in pandas (`df.set_index("time").resample("1D").mean()`) for a coarser cadence.

## Coverage caveat

PVGIS is **not global** — high latitudes and open sea are excluded. A
single out-of-coverage `point=` raises a `ValueError` naming the coordinate; an
out-of-coverage point inside a bbox grid is skipped with a warning while the
in-coverage points are kept.
