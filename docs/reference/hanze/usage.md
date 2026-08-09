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

- `country=` takes an ISO2 code or a list; a value that is not two letters is rejected up front. **Codes follow
  HANZE's own NUTS-aligned spelling** — Greece is `EL` (not `GR`) and the United Kingdom is `UK` (not `GB`); a code
  in the wrong vocabulary simply matches nothing.
- `region=` takes a 5-character NUTS-3 code or a list (a 2-letter country prefix plus three alphanumerics); a
  malformed code is rejected rather than silently matching nothing.
- `flood_type=` is case-insensitive and one of `River`, `Flash`, `Coastal`, `River/Coastal`; an unknown type
  raises with a did-you-mean hint.
- `start=` / `end=` filter on the event `Year` — **sub-year precision is ignored**, so `start="1950-06-01"`
  still keeps a January-1950 flood (the window is `Year >= 1950`). Omitting both returns the whole record.
- A non-global `lat_lim` / `lon_lim` (or `aoi=`) downloads the region shapefile once and keeps the events whose
  affected regions intersect the box. Bbox selection is **mediated by the boundary file's NUTS-3 code coverage**:
  an event is kept when one of its affected codes both sits in the box and is present in
  `Regions_v2024_simplified` (events and boundaries share the 2024 vintage, so this matches in practice). Use
  `country=` / `region=` if you need selection independent of the boundary geometry.

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
**restricted to that box** by bounding-box intersection: an event that touched an in-box region may also list
regions outside it, and those are dropped so the map shows only the affected regions within your query extent. A
region whose extent intersects the box is kept whole (selected, not geometrically trimmed).

## Notes

- **Caching.** The events CSV and the region zip are downloaded once into `path` and reused on the next call, so
  repeated queries into the same directory cost no network.
- **`aggregate=` is refused.** HANZE is tabular / vector, not a gridded raster, so passing `aggregate=` raises
  `NotImplementedError`. Post-process the returned `DataFrame` / `FeatureCollection` directly.
- **Beta data.** The pinned record is `v3.0.1-beta`; treat the figures as provisional.
