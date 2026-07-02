# Using the AirNow backend

This page is the hands-on guide to the `earthlens` AirNow backend —
picking pollutants, building a bounding-box query, and the output
format. For background see the [Introduction](introduction.md); for
credentials see [Authentication](authentication.md); the rendered API is
on the [Reference](airnow.md) page.

> **No extra install.** AirNow needs no optional SDK — it talks to the
> `/aq/data/` REST endpoint through `requests`, already a core earthlens
> dependency. You only need a free API key (see
> [Authentication](authentication.md)).

## 1. A first query

```python
from earthlens import EarthLens

df = EarthLens(
    data_source="airnow",
    variables=["pm25"],            # pollutants, NOT data variables
    start="2026-01-01",
    end="2026-01-02",
    lat_lim=[34.0, 34.3],          # Los Angeles basin
    lon_lim=[-118.5, -118.1],
    path="out/airnow",
    api_key="...",                 # or set AIRNOW_API_KEY
).download()

df.head()
```

`download()` returns the long-format `DataFrame` and also writes it to
`path`. The frame always has the same columns, even when the query
matches nothing — an empty result is a schema-only frame, not a missing
one.

## 2. Selecting pollutants via `variables`

For this backend `variables` is the list of **pollutants**, resolved to
AirNow `parameters` codes through the bundled catalog. AirNow reports the
six criteria pollutants:

```python
from earthlens.airnow import Catalog

sorted(Catalog().pollutants)          # ['co', 'no2', 'o3', 'pm10', 'pm25', 'so2']
Catalog().codes_for(["pm25", "o3"])   # ['PM25', 'OZONE']
```

An unknown name raises with a did-you-mean hint
(`get_pollutant("pm2.5")` → suggests `pm25`).

## 3. The bbox and date window

`lat_lim` / `lon_lim` are a WGS84 bounding box (`[min, max]` each) sent
as AirNow's `BBOX=minLon,minLat,maxLon,maxLat`. `start` / `end` are the
inclusive window (parsed with `fmt`, default `"%Y-%m-%d"`). AirNow
observations are hourly, so a date-granular window `[d, d]` fetches the
full day — `startDate=dT00` through `endDate=dT23`.

## 4. Concentration vs AQI (`data_type`)

AirNow can return the raw concentration, the AQI, or both:

| `data_type` | Returns |
|---|---|
| `"C"` | concentration only (`value` / `units`) |
| `"A"` | AQI only (`aqi` / `category`) |
| `"B"` (default) | both |

AirNow encodes "not reported" as the sentinel `-999`; the backend
scrubs it to `NaN` in `value`, `aqi`, and `category`. Two more filters
are exposed: `monitor_type` (`"permanent"` / `"mobile"` / `"both"`) and
`include_raw_concentrations`.

## 5. Output format

`download()` writes the frame to `path` as CSV by default, or Parquet
with `file_format="parquet"`. The file is named
`airnow_<pollutants>_<start>_<end>.<ext>`. The in-memory frame is also
returned, so it is pleasant to use directly in a notebook.
