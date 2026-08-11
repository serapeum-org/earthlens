# CatRaRE heavy-rainfall events — introduction

[CatRaRE](https://www.dwd.de/EN/ourservices/radarklimatologie/radarklimatologie.html)
(Catalogue of Radar-based Rainfall Events) is DWD's objectively-defined
catalogue of **heavy-rainfall events** over Germany, derived from the RADKLIM
radar climatology, covering 2001–2025. Each event is a space–time cluster of
intense rainfall with attributes such as duration, area, maximum rainfall, and
a severity index. earthlens ships a single `catrare` backend that downloads the
event FileGDB directly from DWD's open-data host (`opendata.dwd.de`, CC-BY-4.0 /
GeoNutzV, no credentials) and returns the events as vector features.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](catrare.md) page.

## Why it matters here

CatRaRE is the **event companion to the RADKLIM grids** (`radar` / radklim). The
gridded product tells you how much rain fell where; CatRaRE distils that into
discrete, comparable *events* — "a 5-year, 80-km² cloudburst on 14 July 2021" —
which is the natural unit for pluvial-flood and heavy-rainfall-impact analysis.

Like the GDACS, Aqueduct, and FLOPROS backends, CatRaRE departs from the gridded
backends in that its output is a **vector table, not a grid**, so the facade
rejects an `aggregate=` argument. It has a light time axis: the archive is
static, so `start` / `end` are optional and, when supplied, simply *filter* the
events by date rather than drive a download loop.

## Thresholds

Two threshold selections are published, chosen with `threshold=`:

- `t5` — events whose rainfall reaches at least a **5-year return period**.
- `w3` — a **severity-weighted** selection (based on the `Eta` severity index).

## Geometry layers

Each threshold's FileGDB carries two layers, chosen with `geometry_layer=`:

- `zones` (default) — the **event-footprint polygons** (`EventZones`).
- `points` — the **maximum-rainfall points** (`RRmaxPoints`), one per event.

## Coordinate system

The FileGDB geometry is stored in the DWD **RADOLAN polar-stereographic** grid
and carries **no embedded CRS**. The backend assigns the RADOLAN projection and
reprojects every result to **EPSG:4326**, so a `lat_lim` / `lon_lim` bounding
box and the returned coordinates are ordinary WGS84 longitude/latitude.

## Licence

CatRaRE is CC-BY-4.0 / GeoNutzV. Cite Deutscher Wetterdienst (DWD), CatRaRE
v2026.01 (RADKLIM). The backend logs the attribution on every download. A CSV of
the same 67 event attributes (without geometry) is published alongside each
FileGDB on the DWD server for non-spatial use.
