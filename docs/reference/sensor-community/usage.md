# Using the Sensor.Community backend

This page is the hands-on guide to the `earthlens` Sensor.Community
backend — picking pollutants, the bbox / date window, and the output.
For background see the [Introduction](introduction.md); for the licence
see [Authentication](authentication.md); the rendered API is on the
[Reference](sensor-community.md) page.

> **No extra install, no credentials.** Both the live API and the
> archive are public; the backend uses only `requests` + `pandas`, which
> are core earthlens dependencies.

## 1. A first query

```python
from earthlens.core import EarthLens

df = EarthLens(
    data_source="sensor-community",
    variables=["pm25", "pm10"],
    start="2026-06-30",
    end="2026-06-30",
    lat_lim=[48.5, 48.9],          # Stuttgart — a sensor-dense area
    lon_lim=[9.0, 9.3],
    path="out/sensor_community",
).download()

df.head()
```

`download()` returns the long-format `DataFrame` and also writes it to
`path`. It emits a `LicenseWarning` (ODbL) on every call.

## 2. Selecting pollutants via `variables`

`variables` is the list of **pollutants**, mapped to CSV columns and
serving sensor types through the bundled catalog:

```python
from earthlens.sensor_community import Catalog

sorted(Catalog().pollutants)
# ['humidity', 'pm1', 'pm10', 'pm25', 'pressure', 'temperature']
Catalog().columns_for(["pm25", "pm10"])      # {'P2': 'pm25', 'P1': 'pm10'}
```

Particulate pollutants come from PM sensors (`sds011`, the `pms*`
family, …); `temperature` / `humidity` / `pressure` come from climate
sensors (`dht22`, `bme280`, …). A single request can mix both — a
temperature + humidity request reads both columns from each DHT22 /
BME280.

## 3. The bbox and date window

`lat_lim` / `lon_lim` are a WGS84 bounding box used to **discover**
active sensors via the live API; `start` / `end` are the inclusive day
range (parsed with `fmt`, default `"%Y-%m-%d"`) fetched from the
archive. Because discovery uses the live snapshot, keep the bbox over an
area with **currently-active** sensors, and prefer **recent** days — the
archive lags real time by a day or so.

> **Pick a sensor-dense bbox and a recent day.** A sparse or offline
> area returns few or no sensors; the backend logs a warning when no
> live sensor of the requested type is found in the bbox.

## 4. Output format

`download()` writes the frame to `path` as CSV by default, or Parquet
with `file_format="parquet"`. The file is named
`sensor_community_<pollutants>_<start>_<end>.<ext>`.
