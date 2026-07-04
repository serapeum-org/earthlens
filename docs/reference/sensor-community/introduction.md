# Sensor.Community — introduction

[Sensor.Community](https://sensor.community/) (formerly luftdaten.info)
is a global grassroots network of **low-cost, citizen-operated air and
climate sensors** — tens of thousands of DIY stations, mostly reporting
particulate matter (PM2.5 / PM10) plus temperature, humidity, and
pressure. It is open data under the ODbL and the densest low-cost
network in many European cities.

This page orients the `earthlens` Sensor.Community backend
(`sensor_community`). For the hands-on walkthrough see [Usage](usage.md);
for the licence / access notes see [Authentication](authentication.md);
the rendered API is the [Reference](sensor-community.md) page.

## What earthlens returns

The backend returns a long-format `pandas.DataFrame` — one row per
reading:

| Column | Meaning |
|---|---|
| `station_id` | sensor id |
| `sensor_type` | reporting sensor type (`SDS011`, `DHT22`, …) |
| `parameter` | pollutant name (`pm25`, `pm10`, `temperature`, …) |
| `datetime_utc` | reading timestamp (tz-aware UTC) |
| `value` | measured value |
| `units` | reporting units (`µg/m³`, `°C`, `%`, `Pa`) |
| `lat` / `lon` | sensor coordinates (WGS84) |
| `provider` | `"sensor.community"` |

This makes it a **`tabular`** backend
(`SensorCommunity.OUTPUT_KIND == "tabular"`), like `earthlens.openaq`.
The facade **rejects an `aggregate=` argument** for it.

## How the fetch works (and its one caveat)

The Sensor.Community **archive** stores one CSV per **(sensor, day)** —
roughly 19,000 files per day — with no bounding-box index. So the
backend works in two steps, mirroring `earthlens.openaq`'s search/fetch:

1. **Discover** — query the live JSON API
   (`data.sensor.community`) for the sensors currently reporting inside
   the request bbox whose type serves a requested pollutant.
2. **Fetch** — download each discovered sensor's per-day archive CSV
   (`archive.sensor.community`) across the date window and parse the
   requested columns; a missing daily file is logged and skipped.

> **Caveat.** Because discovery uses the *live* snapshot, historical
> coverage is limited to sensors that are **currently active** in the
> bbox. A sensor that reported in the past but is offline now will not be
> found.

## Data quality & licence

Readings are crowdsourced from **low-cost sensors** — useful for dense
spatial patterns and trends, but **not reference-grade**, and the data
is licensed under the **ODbL** (attribution + share-alike). Every
`download()` emits a `LicenseWarning` to flag this. For regulatory-grade
observations use `earthlens.airnow` (North America),
`earthlens.eea_aq` (Europe), or `earthlens.openaq` (global).
