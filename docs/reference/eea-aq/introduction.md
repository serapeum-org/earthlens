# EEA air quality — introduction

The [European Environment Agency](https://www.eea.europa.eu/) (EEA)
operates the official European air-quality reporting system: member
states submit **reference-grade monitor observations** under the Air
Quality Directive, which the EEA publishes through its download service.
This backend wraps the [`airbase`](https://pypi.org/project/airbase/)
client over that service.

This page orients the `earthlens` EEA backend (`eea_aq`). For the
hands-on download walkthrough see [Usage](usage.md); for the install /
access notes see [Authentication](authentication.md); the rendered API
is the [Reference](eea-aq.md) page.

## What earthlens returns

The backend returns a long-format `pandas.DataFrame` — one row per
measurement:

| Column | Meaning |
|---|---|
| `station_id` | EEA `Samplingpoint` id (`"DE/SPO-..."`) |
| `country` | ISO2 country parsed from the sampling-point prefix |
| `parameter` | pollutant name (`pm25`, `no2`, …) |
| `datetime_utc` | measurement period start (tz-aware UTC) |
| `value` | measured concentration |
| `units` | reporting units (`ug.m-3`, `mg.m-3`) |
| `agg_type` | EEA aggregation type (`hour`) |
| `validity` / `verification` | EEA data-quality flags |
| `dataset` | the reporting era (`Historical` / `Verified` / `Unverified`) |
| `provider` | `"EEA"` |

This makes EEA a **`tabular`** backend
(`EEA_AQ.OUTPUT_KIND == "tabular"`), like `earthlens.openaq`. The facade
**rejects an `aggregate=` argument** for it.

## Country granularity (no `lat` / `lon`)

The EEA service is queried **per country** (ISO2), not per bbox, and the
delivered Parquet carries **no coordinates** — they live only in a
separate 100+ MB metadata export whose keys do not cleanly join. So this
backend maps the request bbox to the reporting countries whose own
bounding box intersects it (or an explicit `country=`) and returns
**every station in those countries**. Results are country-granular and
have no `lat` / `lon` columns; pass `country=` to be precise. See
[Usage](usage.md).

## Reporting eras (datasets)

The EEA splits its archive into three named datasets by reporting era.
The backend picks the one(s) spanning the requested years automatically:

| Dataset | Years | Meaning |
|---|---|---|
| `Historical` | 2002–2012 | pre-Directive archive |
| `Verified` | 2013–2022 | quality-assured (E1a) |
| `Unverified` | 2023+ | up-to-date, not yet verified (E2a/UTD) |

## Why it matters here

Where `earthlens.airnow` is the North-American reference network and
`earthlens.openaq` is the global aggregator, EEA is the authoritative
**European reference network** — the regulatory monitors behind EU
air-quality assessments.
