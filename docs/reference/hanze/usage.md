# HANZE historical flood impacts — usage

A hands-on walkthrough of the `hanze` backend. Background is on the
[Introduction](introduction.md) page; the rendered API is the [Reference](hanze.md) page. There is also an example
notebook: [Quickstart — floods and impacts](../../examples/hanze/quickstart.ipynb).

No credentials are needed — the Zenodo record is public (CC-BY-4.0).

## Events and impacts (the default)

```python
from earthlens.core import EarthLens

events = EarthLens(
    "hanze",
    start="1950",
    end="2020",
    country=["DE", "NL"],
    path="out",
).download()

events[["Country code", "Year", "Type", "Fatalities", "Losses (real value)"]].head()
```

`download()` returns a `pandas.DataFrame` of the matching floods (and writes it beside the cached source CSV under
`path`). The columns are HANZE's own documented headers.

## Filtering

Every selector is optional and they compose with AND:

```python
# Coastal floods in the Netherlands, whole record
EarthLens("hanze", country="NL", flood_type="Coastal", path="out").download()

# A single NUTS-3 region
EarthLens("hanze", region="DE300", path="out").download()

# A bounding box (resolved through the region geometry)
EarthLens("hanze", lat_lim=[50.5, 53.7], lon_lim=[3.3, 7.3], path="out").download()
```

- `country=` takes an ISO2 code or a list; a value that is not two letters is rejected up front.
- `flood_type=` is case-insensitive and one of `River`, `Flash`, `Coastal`, `River/Coastal`; an unknown type
  raises with a did-you-mean hint.
- `start=` / `end=` filter on the event `Year`. Omitting both returns the whole record.
- A non-global `lat_lim` / `lon_lim` (or `aoi=`) downloads the region shapefile once and keeps the events whose
  affected regions intersect the box.

## Affected-region geometry

Pass `with_geometry=True` to get a pyramids `FeatureCollection` of the affected NUTS-3 regions instead of the
events table:

```python
regions = EarthLens(
    "hanze",
    start="1990",
    end="2020",
    country=["DE", "NL"],
    with_geometry=True,
    path="out",
).download()

regions[["nuts3_code", "region_name", "n_events"]].head()
regions.plot(column="n_events", legend=True)
```

The collection carries `nuts3_code`, `region_name`, `n_events` (how many of the matched floods affected each
region) and `geometry`, in WGS84 (`EPSG:4326`) — the shapefile ships in ETRS89-LAEA (`EPSG:3035`) and is
reprojected for you. It is written to a GeoPackage under `path`.

When you combine `with_geometry=True` with a `lat_lim` / `lon_lim` (or `aoi=`) box, the returned regions are
**clipped to that box**: an event that touched an in-box region may also list regions outside it, and those are
dropped so the map shows only the affected regions within your query extent.

## Notes

- **Caching.** The events CSV and the region zip are downloaded once into `path` and reused on the next call, so
  repeated queries into the same directory cost no network.
- **`aggregate=` is refused.** HANZE is tabular / vector, not a gridded raster, so passing `aggregate=` raises
  `NotImplementedError`. Post-process the returned `DataFrame` / `FeatureCollection` directly.
- **Beta data.** The pinned record is `v3.0.1-beta`; treat the figures as provisional.
