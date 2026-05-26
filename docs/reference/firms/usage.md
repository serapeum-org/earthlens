# Using the FIRMS backend

This page is the hands-on guide to the `earthlens` FIRMS backend —
picking sensors, building a query, the transparent 5-day chunking, the
detection filters, and the output schema. For background see the
[Introduction](introduction.md); for credentials see
[Authentication](authentication.md); the rendered API is on the
[Reference](firms.md) page.

> **No extra install.** Like GDACS, FIRMS needs no optional SDK — it
> talks to the area CSV API through `requests` + `pandas`, both core
> earthlens dependencies. You only need a free `MAP_KEY` (see
> [Authentication](authentication.md)).

## 1. A first query

```python
from earthlens import EarthLens

fc = EarthLens(
    data_source="firms",
    variables=["VIIRS_SNPP_NRT"],   # FIRMS sensor codes, NOT data variables
    start="2024-08-01",
    end="2024-08-07",
    lat_lim=[33.0, 35.0],           # Southern California
    lon_lim=[-119.0, -117.0],
    path="out/firms",
    map_key="...",                  # or set FIRMS_MAP_KEY
).download()

fc.head()
```

`download()` returns the `FeatureCollection` (a `geopandas.GeoDataFrame`,
CRS `EPSG:4326`) and also writes it to `path`. The returned collection
always has the same columns, even when the query matches nothing — an
empty result is a schema-only collection, not a missing one (and nothing
is written for an empty result).

## 2. Selecting sensors via `variables`

For this backend `variables` is the list of **FIRMS sensor codes**, not
data-variable names — an intentional overload of the facade's required
`variables` argument:

```python
variables=["VIIRS_SNPP_NRT", "MODIS_NRT"]   # both sensors, merged
```

An empty list defaults to `["VIIRS_SNPP_NRT"]` (the highest-resolution
current NRT sensor). The curated sensor set (every source FIRMS serves
through the area CSV API):

```python
from earthlens.firms import Catalog

Catalog().codes()
# ['BA_MODIS', 'BA_VIIRS', 'GOES_NRT', 'LANDSAT_NRT', 'MODIS_NRT',
#  'MODIS_SP', 'VIIRS_NOAA20_NRT', 'VIIRS_NOAA20_SP', 'VIIRS_NOAA21_NRT',
#  'VIIRS_SNPP_NRT', 'VIIRS_SNPP_SP']
Catalog().get_sensor("VIIRS_SNPP_NRT").resolution_m      # 375
```

An unknown code raises with a did-you-mean hint
(`get_sensor("MODIS_NR")` → suggests `MODIS_NRT`).

Resolution and schema vary by family: **MODIS** 1 km, **VIIRS** 375 m,
**GOES** ~2 km (geostationary), **LANDSAT** 30 m. `GOES_NRT` reports a
numeric (provider-scale) confidence rather than `l`/`n`/`h`;
`LANDSAT_NRT` reports `l`/`m`/`h` confidence and carries **no FRP or
brightness** column (those degrade to `NaN`). The `BA_MODIS` / `BA_VIIRS`
burned-area collections are exposed too, but the area CSV API serves them
with the same point detection schema as their base MODIS / VIIRS sensor —
see the [Introduction](introduction.md) for the burned-area caveat.

> **NRT vs archive.** `*_NRT` sensors cover only ~2 months; `*_SP`
> sensors hold the archive. Requesting an old window against an `*_NRT`
> sensor returns nothing, so the backend logs a warning naming the
> `*_SP` variant — it never silently swaps your sensor.

## 3. The bbox, date window, and 5-day chunking

`lat_lim` / `lon_lim` are a WGS84 bounding box (`[min, max]` each);
`start` / `end` are the inclusive detection window (parsed with `fmt`,
default `"%Y-%m-%d"`). FIRMS caps each request at **5 days and one
sensor**, so the backend walks the window in ≤5-day chunks and issues
one CSV GET per `(sensor, chunk)` — a 25-day, two-sensor request is
`ceil(25/5) × 2 = 10` GETs. This is transparent: you pass the whole
window and get one merged `FeatureCollection` back. The bbox path
segment is sent in FIRMS `W,S,E,N` order.

## 4. Filtering detections

Two client-side filters ride as explicit keyword arguments (FIRMS has no
server-side equivalent):

```python
EarthLens(
    data_source="firms",
    variables=["VIIRS_SNPP_NRT", "MODIS_NRT"],
    start="2024-08-01", end="2024-08-07",
    lat_lim=[33.0, 35.0], lon_lim=[-119.0, -117.0],
    min_confidence=50,        # keep detections with confidence_pct >= 50
    day_night="D",            # daytime overpasses only
    path="out/firms",
).download()
```

`min_confidence` filters on the **normalised** `confidence_pct` column,
so a single threshold works across both families even though MODIS
reports numeric confidence and VIIRS reports the categorical `l`/`n`/`h`
(mapped to 25/60/90). `day_night` keeps only `"D"` or `"N"` rows.

## 5. The output schema and the confidence note

Every row is one fire pixel; the schema is uniform across sensors:

| Column | Notes |
|---|---|
| `latitude` / `longitude` | WGS84 |
| `acq_datetime` | tz-aware UTC (combines `acq_date` + integer-HHMM `acq_time`) |
| `sensor` | the requested sensor code |
| `confidence` | raw value (numeric for MODIS, `l`/`n`/`h` for VIIRS) |
| `confidence_pct` | normalised 0-100 — filter on this |
| `brightness_k` | from `brightness` (MODIS) or `bright_ti4` (VIIRS) |
| `frp` | fire radiative power (MW) |
| `daynight` | `D` / `N` |
| `geometry` | `Point(longitude, latitude)` |

> **`aggregate=` is rejected.** FIRMS output is `vector`, so the facade
> raises `NotImplementedError` for a non-`None` `aggregate=`. There is no
> gridded reduction of a detection table — post-process the returned
> `GeoDataFrame` directly (e.g. `fc.groupby(fc.acq_datetime.dt.date).size()`).

## 6. Output format

`download()` writes the collection to `path` as a GeoPackage by default,
or GeoJSON with `file_format="geojson"`:

```python
EarthLens(data_source="firms", ..., file_format="geojson").download()
```

The file is named `firms_<sensors>_<start>_<end>.<ext>`. The in-memory
collection is also returned, so it is pleasant to use directly in a
notebook.
