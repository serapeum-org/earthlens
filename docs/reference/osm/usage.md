# OpenStreetMap features — usage

This page walks through fetching OSM features with the `osm` backend. For
background and the named-query list see [Introduction](introduction.md); the
rendered API is the [Reference](osm.md) page.

## Install

The two query SDKs ship behind the `[osm]` extra (imported lazily — the base
package imports without them):

```bash
pip install earthlens[osm]      # pulls overpy + ohsome
```

There are no credentials to configure — both Overpass and ohsome are public.

## Quickstart — current-state hospitals (Overpass)

```python
from earthlens import EarthLens

hospitals = EarthLens(
    data_source="osm",
    variables=["overpass:hospitals"],   # a named query — see below
    lat_lim=[49.40, 49.42],             # a small bbox (degrees)
    lon_lim=[8.67, 8.71],
    path="./out",
).download()

print(len(hospitals), "features")
print(hospitals[["osm_id", "osm_type", "geometry"]].head())
```

`download()` returns a pyramids `FeatureCollection` (a `geopandas.GeoDataFrame`
subclass), so every pandas / geopandas method works on it directly. It also
writes `out/osm_overpass-hospitals.geojson`. Keep the bbox small — Overpass is
shared community infrastructure.

## Quickstart — building history at a snapshot (ohsome)

```python
buildings = EarthLens(
    data_source="osm",
    variables=["ohsome:buildings"],
    lat_lim=[49.40, 49.42],
    lon_lim=[8.67, 8.71],
    start="2020-01-01",                 # ohsome needs a time (see below)
    path="./out",
).download()

print(buildings["@snapshotTimestamp"].iloc[0])   # the history timestamp
```

## Choosing the query — `variables`

For this backend `variables` is the list of **named-query ids**, not
data-variable names. The `<protocol>:` prefix routes the request:

```python
# one named query
EarthLens(data_source="osm", variables=["overpass:roads"], ...)

# several at once — combined into one FeatureCollection
EarthLens(data_source="osm", variables=["overpass:hospitals", "overpass:cafes"], ...)
```

The shipped named queries are `overpass:hospitals`, `overpass:roads`,
`overpass:buildings`, `overpass:cafes`, `overpass:schools`, `ohsome:buildings`,
`ohsome:highways`, and `ohsome:amenities`. List them with
`EarthLens.list_datasets("osm")`. An unknown id raises with a did-you-mean hint.

The facade keys `"osm"`, `"openstreetmap"`, `"overpass"`, and `"ohsome"` all
resolve to the same backend.

## The bbox and the time window

- **bbox** — `lat_lim` / `lon_lim` (degrees). The backend hands the box to each
  protocol in the order it expects (Overpass `S,W,N,E`; ohsome `W,S,E,N`); you
  always pass plain `lat_lim` / `lon_lim`. You can also use the ergonomic
  `aoi=` channel (a bbox, a point + `buffer`, or a geometry).
- **time** — Overpass returns **current state** and ignores `start` / `end`.
  ohsome is **history-aware** and *requires* a time: pass `start=` for a single
  snapshot, or `start=` + `end=` for a range (the backend builds the ohsome
  `time` as `"start/end"`). An ohsome query with no `start` raises a helpful
  `ValueError`.

```python
# ohsome over a multi-year range
EarthLens(
    data_source="osm",
    variables=["ohsome:highways"],
    lat_lim=[49.40, 49.42], lon_lim=[8.67, 8.71],
    start="2016-01-01", end="2022-01-01",
    path="./out",
).download()
```

## Raw query / filter overrides (power users)

When the named queries aren't enough, pass your own:

```python
# raw Overpass QL — {bbox} is filled with the request bbox (S,W,N,E)
EarthLens(
    data_source="osm",
    variables=["overpass:hospitals"],            # still needed to route
    lat_lim=[49.40, 49.42], lon_lim=[8.67, 8.71],
    query='[out:json][timeout:180];(node["tourism"="museum"]({bbox}););out geom;',
    path="./out",
).download()

# raw ohsome filter
EarthLens(
    data_source="osm",
    variables=["ohsome:buildings"],
    lat_lim=[49.40, 49.42], lon_lim=[8.67, 8.71],
    start="2020-01-01",
    filter="leisure=park and geometry:polygon",
    path="./out",
).download()
```

A raw `query=` with no `{bbox}` placeholder is sent verbatim (you supply the
bbox in the QL yourself).

## Other knobs

| Keyword | Meaning | Default |
|---|---|---|
| `endpoint` | Overpass API endpoint URL | `https://overpass-api.de/api/interpreter` |
| `user_agent` | `User-Agent` sent on the Overpass POST (a real one is required) | `earthlens (+…)` |
| `timeout` | Overpass HTTP timeout (s); also the QL `[timeout:N]` budget | `180.0` |
| `file_format` | `"geojson"` or `"gpkg"` | `"geojson"` |

## The returned FeatureCollection

CRS `EPSG:4326`. Overpass features carry `osm_id`, `osm_type`, the element's OSM
tags as columns, and a `Point` / `LineString` / `Polygon` geometry (a node → a
point, an open way → a line, a closed way → a polygon; relations are skipped in
the MVP). ohsome features carry the geometry plus `@osmId`,
`@snapshotTimestamp`, and `@other_tags`. An empty result (a quiet box) comes
back as an empty FeatureCollection with the `osm_id` / `osm_type` schema, not an
error.

### Writing to disk

`download()` writes one file automatically (`osm_<ids>.geojson`). To write it
yourself:

```python
hospitals.to_file("hospitals.geojson", driver="GeoJSON")
hospitals.to_file("hospitals.gpkg", driver="GPKG")
```

## Licensing — you must attribute (ODbL)

OSM is **ODbL 1.0** (share-alike). Every `download()` emits a `LicenseWarning`:

```text
OpenStreetMap data is licensed under the Open Database License (ODbL 1.0),
which carries attribution and share-alike obligations: credit
'(c) OpenStreetMap contributors' ...
```

Credit *"© OpenStreetMap contributors"* and license any derived database you
redistribute under ODbL.

## Aggregation is not supported

OSM output is vector, so the `aggregate=` argument is rejected:

```python
EarthLens(data_source="osm", variables=["overpass:roads"], ...).download(aggregate=cfg)
# NotImplementedError: OSM features are vector, not gridded ...
```

Post-process the returned FeatureCollection directly instead (it is a
GeoDataFrame).

## Out of scope

Bulk OSM PBF / planet or region extracts, and ohsome's aggregation endpoints
(counts / areas over time), are **not** part of this backend — see
[Introduction](introduction.md#out-of-scope-follow-ons). For a large area, query
in small tiles and concatenate, or use a dedicated PBF tool.
