# NREL — introduction

earthlens ships a single `nrel` backend that fetches **solar and wind resource
time series** from the US [NREL](https://www.nrel.gov/) (National Renewable
Energy Laboratory, since 2026 the "National Laboratory of the Rockies") via the
NREL/NLR [Developer Network](https://developer.nlr.gov/) keyed CSV download API.
A request resolves to a **per-coordinate time-series `pandas.DataFrame`**, so
this is a `tabular` backend — not a raster, not a grid of pixels.

Three products ship in the MVP:

- **`nsrdb-psm3`** — the National Solar Radiation Database **GOES Aggregated
  PSM v4** hourly series (global / direct / diffuse irradiance + meteorology),
  one point and one year per call (valid years 1998–2024).
- **`nsrdb-tmy`** — the NSRDB GOES **TMY v4** Typical Meteorological Year, an
  hourly multi-year synthesis (`names=tmy`).
- **`wtk`** — the **WIND Toolkit** hourly modelled wind resource (speed,
  direction, temperature at hub heights), CONUS + offshore.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md) and the product ids on the
[Available products](datasets.md) page; the rendered API is the
[Reference](nrel.md) page.

## Authentication is required

Unlike the keyless backends, NREL needs a **free API key _and_ the email that
registered it**, both sent as query params on every request:

1. Register for a key at **<https://developer.nlr.gov/signup/>** — instant, free,
   no approval wait. You get the key on the confirmation page and by email.
2. Pass them to earthlens as `api_key=` / `email=`, or set the `NREL_API_KEY` /
   `NREL_EMAIL` environment variables.

A missing key or email raises an `AuthenticationError` naming the variable —
the request never goes out without credentials.

## How it works

The Developer Network CSV download API (`https://developer.nlr.gov/api/...`,
`GET`, `...-download.csv`) is hit **directly with `requests`** — no heavy SDK
(`rex` / `h5pyd` / HSDS / `pvlib`), no `xarray`, no `pyramids` / array
dependency (pure `requests` + `pandas`). Each CSV call serves **one point and
one year**, so a request fans out to `points × years`: the backend samples the
location(s) — a single `point=(lat, lon)`, or a bounding box expanded to a point
grid at `spacing_deg` — issues one throttled keyed `GET` per `(point, year)`,
parses each CSV into a long-format frame tagged with `lat` / `lon` / `year` /
`product`, and concatenates them.

```python
from earthlens.earthlens import EarthLens

df = EarthLens(
    data_source="nrel",                 # aliases: "nsrdb", "wind-toolkit"
    variables=["ghi", "dni", "dhi"],
    start="2020-01-01",
    end="2020-12-31",
    point=(39.74, -105.18),             # Denver, Colorado
    api_key="...",                      # or set NREL_API_KEY
    email="you@example.com",            # or set NREL_EMAIL
).download()
# -> a per-coordinate hourly pandas.DataFrame
```

## Three things that shape the backend

- **Output is a per-point time series, not a grid.** NREL already returns the
  resolved hourly / TMY series, so there is no gridded reduction to apply. The
  `EarthLens` facade rejects a non-`None` `aggregate=` for this `tabular`
  backend with `NotImplementedError`. (Resample the returned `DataFrame` in
  pandas for a coarser cadence.)

- **A bbox × years becomes many keyed GETs — throttled and capped.** Each
  `(point, year)` is one CSV request. The backend throttles to the documented
  **≤ 1 request per second** (CSV format is also capped at **5000 requests per
  day**) and retries an HTTP `429` with backoff. Before fetching it warns once
  the fan-out passes a soft threshold and raises a `ValueError` past a hard cap
  (`max_requests`, default 500) — so a country-scale box over many years never
  silently fires thousands of calls. Shrink the box, coarsen `spacing_deg`, or
  narrow the year window.

- **NREL coverage is region-dependent.** NSRDB covers the Americas and parts of
  Asia / Africa / Europe; the WIND Toolkit is CONUS + offshore. An
  out-of-coverage or invalid request returns an HTTP error: for a multi-point
  bbox the point/year is **skipped with a warning** (the in-coverage points are
  kept); for a single explicit point a clear `ValueError` naming the coordinate
  is raised.

## Attribution

NSRDB and WIND Toolkit data are free to use with attribution:

> Data from the NREL National Solar Radiation Database (NSRDB) and WIND Toolkit,
> via the NREL Developer Network (<https://developer.nlr.gov/>). Cite per
> <https://nsrdb.nrel.gov/> and the WIND Toolkit references.

The backend logs this citation once on a successful `download()`.
