# OpenAQ — introduction

[OpenAQ](https://openaq.org/) is a non-profit that aggregates open
air-quality data from **more than 180 monitoring networks worldwide** —
government reference networks (US AirNow / EPA, the European
Environment Agency, the UK AURN), national and regional agencies, and
low-cost community sensors (Sensor.Community) — into one harmonised API.
It is the closest thing to a single global index of ground-station
pollutant measurements, and the working dataset behind a large fraction
of public-health, environmental-justice, and air-quality-forecasting
work.

This page orients the `earthlens` OpenAQ backend. For the hands-on
download walkthrough see [Usage](usage.md); for credentials see
[Authentication](authentication.md); the rendered API is the
[Reference](openaq.md) page.

## What earthlens returns

OpenAQ's value is the **timeseries of measurements** at fixed
ground stations, so the backend returns a long-format
`pandas.DataFrame` — one row per measurement — not a gridded raster:

| Column | Meaning |
|---|---|
| `station_id` | OpenAQ location id of the reporting station |
| `parameter` | pollutant name (`pm25`, `no2`, …) |
| `datetime_utc` | measurement timestamp (tz-aware UTC) |
| `value` | measured concentration |
| `units` | reporting units (`µg/m³`, `ppm`) |
| `lat` / `lon` | station coordinates (WGS84) |
| `provider` | upstream network/source |

This makes OpenAQ the package's first **`tabular`** backend
(`OpenAQ.OUTPUT_KIND == "tabular"`). Because a per-row station table is
not a gridded array, the `EarthLens` facade **rejects an `aggregate=`
argument** for this backend — the time-window raster reducer has no
meaning on point observations. To coarsen a timeseries, use OpenAQ's
own server-side rollups via `temporal_resolution` (see
[Usage](usage.md)).

## Why it matters here

The satellite and reanalysis backends (CAMS / ERA5 via CDS, TROPOMI and
other atmospheric collections via Google Earth Engine) give you
**modelled or remotely-sensed** air-quality fields with full spatial
coverage but indirect measurements. OpenAQ is the complementary
**ground truth**: sparse but direct, instrument-grade station readings.
A common workflow is to validate or bias-correct a satellite/model AQ
product against the OpenAQ stations inside the same bbox and window.

## Things to know up front

- **v3 only.** OpenAQ **API v1 and v2 were retired on 31 January
  2025**; only the v3 API works. This backend targets v3 exclusively.
- **A free API key is required.** Every v3 request needs an
  `X-API-Key`. Register a free key at
  <https://explore.openaq.org/register>; see
  [Authentication](authentication.md).
- **PurpleAir is not available.** OpenAQ **dropped the PurpleAir
  low-cost-sensor network on 11 March 2024**, so PurpleAir data cannot
  be retrieved through OpenAQ at any tier — do not expect it.
- **The free tier is rate-limited.** A continent-sized bbox can fan out
  to thousands of requests; the backend caps the fan-out
  (`max_locations`) and honours `429` / `Retry-After` back-off so a
  large query throttles gracefully rather than failing. See
  [Usage](usage.md).
