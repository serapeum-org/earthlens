# PVGIS — introduction

<img src="../../_images/logos/pvgis.svg" alt="PVGIS (EU JRC) logo" height="60">

earthlens ships a single `pvgis` backend that fetches **solar-radiation and
photovoltaic (PV) time series** from the European Commission JRC
[Photovoltaic Geographical Information System (PVGIS)](https://re.jrc.ec.europa.eu/pvg_tools/)
5.3 non-interactive REST API. A request resolves to a **per-coordinate hourly
`pandas.DataFrame`**, so this is a `tabular` backend — not a raster, not a grid
of pixels.

Two tools ship in the MVP:

- **`seriescalc`** — hourly solar-radiation / PV-power time series over a
  chosen year window (global in-plane irradiance, sun height, air temperature,
  wind; PV system power when the PV knobs are set).
- **`tmy`** — a Typical Meteorological Year: an hourly multi-year synthesis of
  temperature, humidity, horizontal / beam / diffuse irradiance, infrared,
  wind, and surface pressure.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md) and the product ids on the
[Available datasets](datasets.md) page; the rendered API is the
[Reference](pvgis.md) page.

## How it works

PVGIS 5.3 is a **keyless** REST API (`https://re.jrc.ec.europa.eu/api/v5_3/<tool>`,
`GET`, `outputformat=json`). The backend hits it **directly with `requests`** —
there is no SDK, no authentication, and no `pyramids` / array dependency (pure
`requests` + `pandas`). A request samples the location(s) — a single
`point=(lat, lon)`, or a bounding box expanded to a point grid at `spacing_deg`
— issues one keyless `GET` per point, parses each JSON response into a
long-format frame tagged with `lat` / `lon` / `product`, and concatenates them.

```python
from earthlens.core import EarthLens

df = EarthLens(
    data_source="pvgis",            # topic key: "pvgis:solar-pv"
    variables=["seriescalc"],
    start="2020-01-01",
    end="2020-12-31",
    point=(45.0, 8.0),              # northern Italy
).download()
# -> a per-coordinate hourly pandas.DataFrame
```

## Three things that shape the backend

- **Output is a per-point time series, not a grid.** PVGIS already returns the
  resolved hourly / TMY series, so there is no gridded reduction to apply. The
  `EarthLens` facade rejects a non-`None` `aggregate=` for this `tabular`
  backend with `NotImplementedError`. (Resample the returned `DataFrame` in
  pandas if you want a coarser cadence.)

- **A bbox becomes many keyless GETs — throttled and capped.** A bounding box
  is sampled to a `(lat, lon)` grid at `spacing_deg` (default `0.1°`) and each
  point is one request. The backend throttles to the documented **30 requests
  per second** limit and retries an HTTP `429` with exponential backoff. Before
  fetching it warns once the grid passes a soft threshold and raises a
  `ValueError` past a hard cap (`max_points`, default 400) — so a country-scale
  box never silently fires thousands of calls. Shrink the box or coarsen
  `spacing_deg`.

- **PVGIS is not global.** Coverage spans Europe, Africa, most of Asia, and the
  Americas, but excludes high latitudes and open sea. An out-of-coverage point
  returns an HTTP 4xx with a JSON `message`: for a multi-point bbox the point is
  **skipped with a warning** (the in-coverage points are kept); for a single
  explicit point a clear `ValueError` naming the coordinate is raised. A skipped
  point reflects whatever error PVGIS returned for it (most often
  out-of-coverage, but the warning quotes the actual `message`).

## Attribution

PVGIS data is free to reuse with attribution:

> PVGIS © European Union, 2001–2024 — data from the JRC Photovoltaic
> Geographical Information System (PVGIS),
> <https://re.jrc.ec.europa.eu/pvg_tools/>.

The backend logs this citation once on a successful `download()`.
