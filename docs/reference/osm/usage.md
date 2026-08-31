# OpenStreetMap features — usage

This page walks through fetching OSM features with the `osm` backend. For
background and the named-query list see [Introduction](introduction.md); the
rendered API is the [Reference](osm.md) page.

## Install

The protocol SDKs ship behind two extras (imported lazily — the base package
imports without them):

```bash
pip install earthlens[osm]      # overpy + ohsome  (Overpass + ohsome protocols)
pip install earthlens[osm-pbf]  # pyrosm + osmium  (the pbf protocol)
```

`[osm-pbf]` is **not** in `[all]` because `pyrosm` pulls the sdist-only `cykhash`
(it would need a C compiler), so install it explicitly for bulk PBF work. Note
`pyosmium` is published on PyPI as `osmium`. There are no credentials to
configure — Overpass, ohsome, and Geofabrik are all public.

## Quickstart — current-state hospitals (Overpass)

```python
from earthlens.core import EarthLens

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

## Quickstart — every building in a region (pbf)

For a **bulk** ask — every building in a country — use the `pbf` protocol. It
downloads a [Geofabrik](https://download.geofabrik.de/) extract for `region=`
(cached on disk), reads the layer with `pyrosm`, and clips to the request bbox:

```python
buildings = EarthLens(
    data_source="osm",
    variables=["pbf:buildings"],
    region="malta",                     # a Geofabrik region key (or "europe/andorra")
    lat_lim=[35.88, 35.94],             # bbox clips the read; omit for the whole extract
    lon_lim=[14.48, 14.54],
    path="./out",
).download()

print(len(buildings), "building footprints")
```

The first call downloads the extract (Malta is ~8.8 MB) to a cross-run cache
(`osm_pbf/` under the shared earthlens cache directory by default — see
[Configuration](../configuration.md) — override with `cache_dir=`); repeat
calls reuse it. List the region keys with `Catalog().region_ids()`, or pass a
raw Geofabrik path (any string with a `/`). Omit `lat_lim` / `lon_lim` to read
the whole extract — the bbox-area cap does **not** apply to a `pbf` read.

!!! note "Engines — `pyrosm` (default) vs `pyosmium`"
    `engine="pyrosm"` (the default) reads the whole extract in memory and gives
    the richest columns; it refuses a file over 4 GB. For a **continent- or
    planet-scale** extract, pass `engine="pyosmium"` to stream it with bounded
    memory. The backend warns before downloading a multi-GB extract. Never load
    `planet.osm` with `pyrosm`.

    The `pyosmium` engine is a **coarser fallback**: it returns a slimmer
    `osm_id` / `osm_type` / `geometry` schema and, per layer, a single geometry
    kind under one representative tag (so it under-reports a row's advertised
    `geometry_types` — e.g. `pbf:pois` yields only node points, `pbf:roads`
    approximates `network_type="driving"` rather than reproducing `pyrosm`'s
    exact filter). Use `pyrosm` when you need the full, exact per-layer output.

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
`ohsome:highways`, `ohsome:amenities`, and the `pbf:*` layers (`pbf:buildings`,
`pbf:roads`, `pbf:pois`, `pbf:landuse`, `pbf:natural`, `pbf:boundaries`). List
them with `EarthLens.list_datasets("osm")`. An unknown id raises with a
did-you-mean hint.

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
bbox in the QL yourself). A raw Overpass `query=` **must request JSON output**
(`[out:json]`) — the response is parsed with `overpy.Overpass().parse_json`, so
an `[out:xml]` / `[out:csv]` override will not parse.

## Other knobs

| Keyword | Meaning | Default |
|---|---|---|
| `endpoint` | Overpass API endpoint URL | `https://overpass-api.de/api/interpreter` |
| `user_agent` | `User-Agent` sent on the Overpass POST (a real one is required) | `earthlens (+…)` |
| `timeout` | Overpass HTTP timeout (s); also the QL `[timeout:N]` budget | `180.0` |
| `file_format` | `"geojson"` or `"gpkg"` | `"geojson"` |
| `max_bbox_deg2` | bbox-area cap (square degrees) — guards the planet-wide footgun (live protocols only) | `100.0` |
| `region` | Geofabrik region key or raw path — **required** for a `pbf:*` query | `None` |
| `engine` | `pbf` read engine: `"pyrosm"` (in-memory) or `"pyosmium"` (streaming) | `"pyrosm"` |
| `cache_dir` | directory for cached `.osm.pbf` extracts | `<cache_dir()>/osm_pbf` |

!!! warning "Keep the bbox small (live protocols)"
    Overpass / ohsome are for small/targeted queries. A box larger than
    `max_bbox_deg2` (the default `100` square degrees comfortably covers a large
    country) is rejected before any request — in particular the whole-Earth
    default you get if you omit `lat_lim` / `lon_lim` through the facade, which
    would hammer the shared public services. Raise `max_bbox_deg2=` for a
    genuinely larger area. The cap does **not** apply to a `pbf` read (it hits a
    local extract, not a shared service), so a `pbf` request with no bbox simply
    reads the whole downloaded extract.

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

ohsome's aggregation endpoints (counts / areas over time) are **not** part of
this backend — see
[Introduction](introduction.md#out-of-scope-follow-ons). For bulk asks, reach
for the `pbf` protocol (above) rather than tiling many live queries.
