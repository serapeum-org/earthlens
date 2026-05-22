# Using the OpenAQ backend

This page is the hands-on guide to the `earthlens` OpenAQ backend —
picking pollutants, building a query, the server-side rollups, and the
fan-out caps that keep a large request inside the free rate limit. For
background see the [Introduction](introduction.md); for credentials see
[Authentication](authentication.md); the rendered API is on the
[Reference](openaq.md) page.

> **No extra install.** Unlike the other backends, OpenAQ needs no
> optional SDK — it talks to the v3 REST API through `requests`, which
> is already a core earthlens dependency. You only need a free API key
> (see [Authentication](authentication.md)).

## 1. A first query

```python
from earthlens import EarthLens

df = EarthLens(
    data_source="openaq",
    variables=["pm25"],            # pollutant parameters, NOT data variables
    start="2024-01-01",
    end="2024-01-31",
    lat_lim=[34.0, 34.3],          # Los Angeles basin
    lon_lim=[-118.5, -118.1],
    path="out/openaq",
    api_key="...",                 # or set OPENAQ_API_KEY
).download()

df.head()
```

`download()` returns the long-format `DataFrame` and also writes it to
`path`. The returned frame always has the same columns
(`station_id`, `parameter`, `datetime_utc`, `value`, `units`, `lat`,
`lon`, `provider`), even when the query matches nothing — an empty
result is a schema-only frame, not a missing one.

## 2. Selecting pollutants via `variables`

For this backend `variables` is the list of **pollutant parameters**,
not data-variable names — an intentional overload of the facade's
required `variables` argument:

```python
variables=["pm25", "no2"]          # PM2.5 and NO2 from every matching station
```

Names are resolved to OpenAQ numeric `parameters_id` through the
bundled catalog. The curated set covers the criteria pollutants and a
few meteorological parameters:

```python
from earthlens.openaq import Catalog

sorted(Catalog().parameters)
# ['bc', 'bc_370', 'bc_375', 'bc_470', 'bc_528', 'bc_625', 'bc_880',
#  'ch4', 'co', 'co2', 'humidity', 'no', 'no2', 'nox', 'o3', 'pm1',
#  'pm10', 'pm25', 'pm4', 'pressure', 'relativehumidity', 'so2',
#  'temperature', 'ufp', 'um003', 'um010', 'um025', 'um100',
#  'wind_direction', 'wind_speed']
Catalog().ids_for(["pm25", "no2"])      # [2, 15]
```

An unknown name raises with a did-you-mean hint
(`get_parameter("pm2.5")` → suggests `pm25`).

## 3. The bbox and date window

`lat_lim` / `lon_lim` are a WGS84 bounding box (`[min, max]` each);
`start` / `end` are the inclusive measurement window (parsed with
`fmt`, default `"%Y-%m-%d"`). OpenAQ fetches the whole window per
sensor in one paginated call, so there is no per-day loop.

## 4. `temporal_resolution` — server-side rollups

OpenAQ can roll measurements up **server-side**, which returns far
fewer rows than raw readings and is the natural use of
`temporal_resolution` here:

| `temporal_resolution` | OpenAQ endpoint | rows for a 1-year window |
|---|---|---|
| `"hourly"` | `/sensors/{id}/hours` | ~8,760 per sensor |
| `"daily"` (default) | `/sensors/{id}/days` | ~365 per sensor |
| `"monthly"` | `/sensors/{id}/months` | ~12 per sensor |
| `"yearly"` | `/sensors/{id}/years` | ~1 per sensor |
| `"all"` / `"raw"` | `/sensors/{id}/measurements` | every raw reading |

The default is `"daily"` (matching the facade default), so a facade
user who omits `temporal_resolution` gets the **daily rollup**, not raw
readings. Pass `temporal_resolution="all"` for raw measurements.

> This server-side rollup is **not** the `aggregate=` mechanism. OpenAQ
> output is `tabular`, so the facade rejects `aggregate=` with
> `NotImplementedError`; coarsen the timeseries with
> `temporal_resolution` instead.

## 5. Fan-out caps and rate limits

The natural query is three levels deep — locations → each location's
sensors → each sensor's measurements — so a country- or
continent-sized bbox can be **hundreds-to-thousands of requests**, and
OpenAQ's free tier rate-limits (returning `429` with a `Retry-After`
header). The backend protects you two ways:

- **`max_locations`** (default `500`) caps how many monitoring
  locations are enumerated; **`max_sensors_per_location`** caps sensors
  per location. When a cap truncates the result, a loud warning is
  logged so you know the frame is partial. Raise the caps or shrink the
  bbox/window to capture every station.
- **Automatic back-off.** Every request honours `429` / `Retry-After`
  with capped exponential back-off, so a large query throttles
  gracefully instead of failing.

```python
EarthLens(
    data_source="openaq",
    variables=["pm25"],
    start="2024-01-01", end="2024-12-31",
    lat_lim=[24.0, 49.0], lon_lim=[-125.0, -66.0],   # CONUS — large!
    temporal_resolution="daily",                      # rollup, not raw
    max_locations=200,                                # bound the fan-out
    path="out/openaq",
).download()
```

## 6. Output format

`download()` writes the frame to `path` as CSV by default, or Parquet
with `file_format="parquet"`:

```python
EarthLens(data_source="openaq", ..., file_format="parquet").download()
```

The file is named `openaq_<parameters>_<start>_<end>.<ext>`. The
in-memory frame is also returned, so it is pleasant to use directly in
a notebook.
