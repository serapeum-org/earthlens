# USGS Water — Usage

Every request goes through the unified `EarthLens` facade with
`data_source="usgs-water"` (aliases `"usgs-nwis"`, `"nwis"`), or you can use
the `earthlens.usgs_water.USGSWater` backend directly.

## Request shape

```python
from earthlens.earthlens import EarthLens

df = EarthLens(
    data_source="usgs-water",
    variables=["discharge", "gage_height"],   # codes or friendly names
    start="2023-01-01",
    end="2023-01-31",
    lat_lim=[38.9, 39.0],                      # [lat_min, lat_max]
    lon_lim=[-77.2, -77.0],                    # [lon_min, lon_max]
    path="out",
    service="daily",
    api="auto",
    output_format="csv",
).download(progress_bar=False)
```

### Constructor keyword arguments

| Argument | Default | Meaning |
|---|---|---|
| `variables` | `["00060"]` | NWIS parameter codes **or** friendly names; resolved to 5-digit codes via the [catalog](datasets.md). A raw 5-digit code passes through unmapped. Ignored by the site-keyed services. |
| `start`, `end` | — | Inclusive date window, parsed with `fmt` (`"%Y-%m-%d"`). |
| `lat_lim`, `lon_lim` | — | WGS84 bounding box. Mapped to `west,south,east,north` for the query. |
| `service` | `"daily"` | The NWIS / Water Data plane (see below). |
| `sites` | `None` | Explicit USGS site number(s) (e.g. `"01646500"`), bypassing the bbox. **Required** for `peaks` / `ratings`. |
| `api` | `"auto"` | Endpoint selector — `"auto"` / `"waterdata"` / `"legacy"` (see [Authentication](authentication.md)). |
| `api_token` | `None` | Optional USGS PAT; falls back to `API_USGS_PAT`, then anonymous. |
| `output_format` | `"csv"` | `"csv"` or `"parquet"`. |
| `stat_type` | `"daily"` | For `service="statistics"` — `"daily"` / `"monthly"` / `"annual"`. |
| `limit` | `None` | Optional cap on rows pulled per request (modern `limit=`). |
| `temporal_resolution` | `"daily"` | Convenience alias: a sub-daily value maps the *default* service to `instantaneous`. An explicit `service=` always wins. |

The output is written to `out/usgs_{service}_{codes}.{csv,parquet}` and the
`DataFrame` is returned.

## Selecting by site vs bbox

```python
# By bbox (discovers sites server-side):
EarthLens(data_source="usgs-water", variables=["discharge"],
          lat_lim=[38.9, 39.0], lon_lim=[-77.2, -77.0], start=..., end=...)

# By explicit site number(s):
EarthLens(data_source="usgs-water", variables=["discharge"], sites="01646500",
          lat_lim=[0, 0], lon_lim=[0, 0], start=..., end=...)
```

Note: the modern `instantaneous` endpoint has no bbox filter, so a bbox-only
instantaneous query under `api="auto"` is served by the legacy endpoint; pass
`sites=` to use the modern endpoint.

## The services

### Values — `daily`, `instantaneous`, `gwlevels`

Long table of `site_no, datetime, parameter_code, parameter_name, value, unit,
qualifier, statistic_id`.

```python
EarthLens(data_source="usgs-water", service="instantaneous",
          variables=["00060"], sites="01646500", start=..., end=...).download()
```

`gwlevels` is groundwater (parameter family `72019`, served by the
daily/continuous endpoints).

### Water quality — `samples`

Discrete lab/field results (the WQP samples profile). Columns: `site_no,
datetime, parameter_code, characteristic, value, unit, qualifier,
detection_condition, detection_limit, detection_limit_unit, method, fraction,
medium`. **Modern-only** — needs a token for anonymous use (see
[Authentication](authentication.md)).

```python
EarthLens(data_source="usgs-water", service="samples",
          variables=["dissolved_oxygen"], sites="01646500",
          start="2018-01-01", end="2018-12-31").download()
```

### Statistics — the server-side rollup

`service="statistics"` is the proper answer to "aggregate this series" (the
facade rejects `aggregate=` for tabular backends). Columns: `site_no,
parameter_code, parameter_name, time_of_year, value, percentile, statistic,
unit`.

```python
EarthLens(data_source="usgs-water", service="statistics", stat_type="monthly",
          variables=["discharge"], sites="01646500",
          start="2020-01-01", end="2021-12-31").download()
```

### Specialized hydrology — `peaks`, `ratings`, `field-measurements`

* `peaks` — annual peak streamflow (`site_no, datetime, peak_value,
  gage_height, qualifier`). **Site-keyed**: pass `sites=`.
* `ratings` — stage-discharge rating curve (`stage, discharge, storage`).
  **Site-keyed**: pass `sites=`.
* `field-measurements` — discrete field measurements (modern-only).

```python
EarthLens(data_source="usgs-water", service="peaks", sites="01646500",
          variables=["discharge"], start="1990-01-01", end="2020-12-31").download()
```

### Site discovery — `sites`

`service="sites"` returns station metadata for the bbox: `site_no,
station_name, latitude, longitude, huc, site_type`. Use it to enumerate
coverage before pulling values.

```python
EarthLens(data_source="usgs-water", service="sites", variables=["discharge"],
          lat_lim=[38.9, 39.0], lon_lim=[-77.2, -77.0],
          start="2023-01-01", end="2023-01-05").download()
```

## Why `aggregate=` is rejected

USGS output is tabular per-site rows, not a gridded raster, so there is no
meaningful gridded reduction. The facade raises `NotImplementedError` for a
non-`None` `aggregate=`. Use `service="statistics"` for a server-side temporal
rollup instead.

## Rate limits

The modern endpoint throttles anonymous requests (HTTP 429). The default
`api="auto"` falls back to the legacy endpoint on a 429; set `API_USGS_PAT`
to use the modern endpoint without throttling. See
[Authentication](authentication.md).
