# USGS Water — Introduction

<img src="../../_images/logos/usgs-water.svg" alt="USGS National Water Information System logo" height="60">

`earthlens.usgs_water` wraps the official
[`dataretrieval`](https://github.com/DOI-USGS/dataretrieval-python) SDK to pull
water observations from the U.S. Geological Survey's **National Water
Information System (NWIS)** and the modern **USGS Water Data API** — roughly
10,000 active stream gauges plus groundwater wells and water-quality sites
across the United States and its territories, with records reaching back
decades.

It is a **tabular** backend: every request returns a long-format
`pandas.DataFrame` (one row per observation), so `USGSWater.OUTPUT_KIND` is
`"tabular"` and the `EarthLens` facade rejects an `aggregate=` argument for it
(use the server-side `service="statistics"` rollup instead — see
[Usage](usage.md)).

## Install

```bash
pip install earthlens[usgs-water]
```

This pulls the `dataretrieval` SDK. The package imports without the extra; the
SDK is only needed when you call `download()`.

## What you get

A request is a **bbox** (or explicit `sites=`) + a **time window** + a list of
**NWIS parameter codes** (or friendly names). The result is one tidy long
table you can write to CSV or Parquet.

```python
from earthlens.core import EarthLens

df = EarthLens(
    data_source="usgs-water",      # aliases: "usgs-nwis", "nwis"
    variables=["discharge"],        # friendly name or raw code "00060"
    start="2023-01-01",
    end="2023-01-31",
    lat_lim=[38.9, 39.0],
    lon_lim=[-77.2, -77.0],
    path="out",
    service="daily",                # the default
).download()
```

## The service surface

A single `service=` keyword selects which NWIS / Water Data plane to query
(default `"daily"`):

| `service=` | what it returns |
|---|---|
| `daily` | daily values per site / parameter |
| `instantaneous` | sub-daily / continuous values |
| `samples` | discrete water-quality results (the WQP samples profile) |
| `statistics` | daily / monthly / annual statistical summaries (the server-side rollup) |
| `gwlevels` | groundwater levels (a parameter family served by the values endpoints) |
| `field-measurements` | discrete field measurements |
| `peaks` | annual peak streamflow (site-keyed) |
| `ratings` | stage-discharge rating curves (site-keyed) |
| `sites` | station discovery + metadata |

See [Available data](datasets.md) for the parameter catalog and
[Usage](usage.md) for each service's request shape.

## The modern / legacy migration

The USGS is migrating from the legacy `waterservices.usgs.gov` endpoint (the
`dataretrieval.nwis` module) to the modern `api.waterdata.usgs.gov` endpoint
(the `dataretrieval.waterdata` module). The backend's `api=` keyword chooses:

* **`"auto"`** (default) — try the modern endpoint, but because it
  rate-limits anonymous access aggressively (HTTP 429), transparently fall
  back to the legacy endpoint on a 429 when no token is set.
* **`"waterdata"`** — force the modern endpoint (a 429 surfaces as an error).
* **`"legacy"`** — force the legacy endpoint (reliable anonymously today; the
  USGS plans to retire it on or after 2027-05-06).

A few services are **modern-only** in current `dataretrieval` (`samples` and
`field-measurements` lost their legacy functions), so anonymous use of those
needs a token — see [Authentication](authentication.md).
