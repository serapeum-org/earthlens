# CatRaRE heavy-rainfall events — usage

The `catrare` backend needs **no credentials** — the CatRaRE FileGDBs are public
on `opendata.dwd.de`. A request selects a threshold and, optionally, a date
window and/or a bounding box; the downloaded FileGDB is cached, so repeated
requests reuse it.

For the concepts (what the data is, the thresholds, the CRS) see
[Introduction](introduction.md); the rendered API is the [Reference](catrare.md)
page.

## Quick start

```python
from earthlens.core import EarthLens

# Every T5 heavy-rainfall event (footprint polygons), reprojected to WGS84.
fc = EarthLens(
    "catrare",
    threshold="t5",
    path="catrare-data",
).download()

fc.head()  # a FeatureCollection: Event_ID, Date_START, …, Area, Eta, geometry
```

`download()` returns a pyramids `FeatureCollection` (a `geopandas.GeoDataFrame`
subclass) in EPSG:4326 and also writes it to a GeoPackage under `path`.

## Filtering by date and area

```python
# The July 2021 flood over west Germany (Ahr / NRW).
ahr = EarthLens(
    "catrare",
    threshold="t5",
    start="2021-07-01",
    end="2021-07-31",
    lat_lim=[50.0, 51.5],
    lon_lim=[6.0, 8.0],
).download()
```

`start` / `end` keep events whose `[Date_START, Date_END]` interval overlaps the
window (either bound may be omitted). The bounding box keeps events whose
geometry intersects it — the coordinates are ordinary WGS84 because the backend
reprojects off the DWD RADOLAN grid.

## Thresholds and geometry layers

```python
# The severity-weighted selection, as maximum-rainfall points instead of zones.
pts = EarthLens(
    "catrare",
    threshold="w3",
    geometry_layer="points",
).download()
```

`threshold=` is `"t5"` (return period ≥ 5 yr) or `"w3"` (severity-weighted);
`geometry_layer=` is `"zones"` (event-footprint polygons, default) or `"points"`
(one maximum-rainfall point per event).

## Tabular output

Pass `geometry=False` to drop the geometry and get a plain `pandas.DataFrame` of
the events and their 13 core attributes:

```python
table = EarthLens("catrare", threshold="t5", geometry=False).download()
```
