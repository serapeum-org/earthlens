# AirNow — introduction

[AirNow](https://www.airnow.gov/) is the US EPA's official real-time
air-quality programme, run jointly with NOAA, the National Park Service,
tribal, state, and local agencies, and Environment Canada. It publishes
**reference-grade hourly monitor observations** — the regulatory-quality
instruments behind the US Air Quality Index — across the United States
and Canada.

This page orients the `earthlens` AirNow backend. For the hands-on
download walkthrough see [Usage](usage.md); for credentials see
[Authentication](authentication.md); the rendered API is the
[Reference](airnow.md) page.

## What earthlens returns

AirNow's value is the **timeseries of observations** at fixed monitoring
sites, so the backend returns a long-format `pandas.DataFrame` — one row
per measurement — not a gridded raster:

| Column | Meaning |
|---|---|
| `station_id` | AirNow `FullAQSCode` of the monitoring site |
| `parameter` | pollutant as AirNow reports it (`PM2.5`, `OZONE`, …) |
| `datetime_utc` | observation timestamp (tz-aware UTC) |
| `value` | measured concentration (AirNow's `Value`) |
| `raw_value` | unadjusted concentration (`RawConcentration`; `NaN` unless `include_raw_concentrations=True`) |
| `units` | reporting units (`UG/M3`, `PPB`, `PPM`) |
| `aqi` | the reported Air Quality Index (NaN when not reported) |
| `category` | AQI category number (1–6) |
| `lat` / `lon` | site coordinates (WGS84) |
| `site_name` | monitoring-site name |
| `provider` | reporting agency (`AgencyName`) |

This makes AirNow a **`tabular`** backend
(`AirNow.OUTPUT_KIND == "tabular"`), like `earthlens.openaq`. Because a
per-row station table is not a gridded array, the `EarthLens` facade
**rejects an `aggregate=` argument** for this backend — the time-window
raster reducer has no meaning on point observations.

## Why it matters here

Where `earthlens.openaq` is the global aggregator and
`earthlens.eea_aq` covers Europe, AirNow is the authoritative
**North-American reference network** — the same regulatory monitors that
define the official US AQI, without the aggregation layer in between. A
common workflow is to validate or bias-correct a satellite / model AQ
product (TROPOMI, ERA5) against the AirNow monitors inside the same bbox
and window.

## Things to know up front

- **A free API key is required.** Every `/aq/data/` request needs an
  `API_KEY`. Register a free key at
  <https://docs.airnowapi.org/account/request/>; see
  [Authentication](authentication.md).
- **Bounding-box endpoint.** This backend uses AirNow's
  `/aq/data/` bounding-box service, which matches earthlens's
  `lat_lim` / `lon_lim` model. AirNow's zip-code / lat-long observation
  endpoints are **retiring in Fall 2026** and are not used.
- **Hourly monitor data.** `/aq/data/` returns hourly observations;
  `temporal_resolution` is recorded for provenance but does not change
  the request.
- **North America only.** AirNow covers the US and Canada; for Europe
  use `earthlens.eea_aq`, and for global coverage `earthlens.openaq`.
