# GDACS multi-hazard alerts — introduction

<img src="../../_images/logos/gdacs.png" alt="GDACS logo" height="60">

The [GDACS](https://www.gdacs.org/) (Global Disaster Alert and
Coordination System) is a cooperation framework of the United Nations
and the European Commission (run by the JRC together with UN OCHA) that
issues near-real-time **multi-hazard disaster alerts** with a simple
green / orange / red impact score. earthlens ships a single `gdacs`
backend that reads its public GeoJSON feed over plain HTTPS — one
request returns every alert in a date window across all six hazard
types.

This page orients the backend. For the hands-on download walkthrough
see [Usage](usage.md); the rendered API is the [Reference](gdacs.md)
page.

## Why it matters here

Like the FDSN seismic backend, GDACS departs from the gridded backends
(CHC rainfall, ERA5, GEE imagery) in two ways that shape it:

- **The output is a vector table, not a grid.** A GDACS query returns a
  list of point features — one row per alert, each with a hazard type,
  an alert level, an impact score, a country, dates, and a geometry
  (usually a `Point` event centroid). So GDACS is a **`vector`** backend
  (`GDACS.OUTPUT_KIND == "vector"`), and `download()` returns a
  [pyramids](https://github.com/serapeum-org/pyramids)
  `FeatureCollection` (a `geopandas.GeoDataFrame` subclass) rather than
  writing a raster. Because there is no meaningful gridded reduction of
  an alert table, the `EarthLens` facade rejects an `aggregate=`
  argument for this backend with `NotImplementedError`.

- **There is no dataset catalog to curate.** GDACS is a *fixed alert
  feed*, not an archive of datasets. The entire "catalog" is a six-row
  table mapping each GDACS hazard-type code to a display name. There is
  no `refresh` / `probe` / `audit` tooling — the six codes are the whole
  GDACS universe.

## The hazard types

| Code (`variables=[...]`) | Hazard | Severity reported as |
|---|---|---|
| `EQ` | Earthquake | moment magnitude (`severityunit` `"M"`) |
| `TC` | Tropical cyclone | maximum sustained wind |
| `FL` | Flood | GDACS flood magnitude score |
| `VO` | Volcano | eruption / unrest indicator |
| `WF` | Wildfire | burned area |
| `DR` | Drought | drought severity score |

Each is selected by passing its code in `variables` — for this backend
`variables` is the list of **hazard types**, not data-variable names (an
intentional, documented overload; see [Usage](usage.md)). An empty list
defaults to all six. The alert-level filter (`Green` / `Orange` /
`Red`) is the separate `alert_level=` keyword.

## Authentication

**None.** The GDACS feed is fully public — no key, no token, no login.
GDACS is the first credential-free earthlens backend since CHC; there is
no `authentication.md` page because there is nothing to authenticate,
and no `[gdacs]` extra to install (the only dependency is `requests`, a
core dependency).

## What a query returns

One `FeatureCollection` (CRS `EPSG:4326`) with these columns:

| Column | Type | Meaning |
|---|---|---|
| `event_id` | str | GDACS event identifier |
| `episode_id` | str | episode identifier (an event can have several episodes) |
| `hazard_type` | str | one of `EQ` / `TC` / `FL` / `VO` / `WF` / `DR` |
| `name` | str | human-readable alert name |
| `alert_level` | str | `Green` / `Orange` / `Red` |
| `alert_score` | float | numeric impact score |
| `from_date` / `to_date` | datetime (UTC) | alert validity window |
| `country` | str | affected country name |
| `iso3` | str | ISO-3166 alpha-3 country code |
| `glide` | str | GLIDE identifier (joins to ReliefWeb / Copernicus EMS) |
| `severity` | float | hazard-specific magnitude (may be null) |
| `severity_unit` | str | unit of `severity` (e.g. `"M"` for an earthquake) |
| `severity_text` | str | human-readable severity description |
| `geometry` | Point/Polygon | event geometry (`shapely`), CRS `EPSG:4326` |

The `glide` and `iso3` columns are join keys: GLIDE ids cross-reference
the same disaster in [ReliefWeb](https://reliefweb.int/) and
[Copernicus EMS](https://emergency.copernicus.eu/), and `iso3` joins to
country-level datasets. As a side effect, `download()` also writes the
collection to one vector file in the output directory (GeoPackage by
default, or GeoJSON).

!!! note "100-event cap"
    The SEARCH endpoint returns at most the **100 most-recent events**
    per request and offers no pagination. A busier window is truncated
    upstream; the backend logs a warning when a response hits the cap.
    To get everything, narrow the date window or query fewer hazard
    types — see [Usage](usage.md#result-size-limit-100-events).

## Alerts, not an authoritative catalog

GDACS is an **impact-alert** feed: it answers "what disasters are
happening and how bad are they?", optimised for speed and coordination,
not scientific completeness. For a rigorous per-domain catalog use the
sibling backends instead — **seismic events → FDSN** (the reviewed
USGS / EMSC / ISC bulletins), not GDACS's earthquake alerts. Treat the
GDACS `severity` as an alerting indicator, not a definitive measurement.

## Cost

**Free.** The GDACS feed is public, operated by the JRC. Recommended
attribution and terms are documented on the GDACS site.

## References

- GDACS: <https://www.gdacs.org/>
- GDACS about / methodology: <https://www.gdacs.org/about.aspx>
- GLIDE disaster identifiers: <https://glidenumber.net/>
- earthlens GDACS usage: [Usage](usage.md)
- earthlens GDACS API: [Reference](gdacs.md)
