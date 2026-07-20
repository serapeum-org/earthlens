# OpenStreetMap features — introduction

<img src="../../_images/logos/osm.svg" alt="OpenStreetMap logo" height="60">

[OpenStreetMap](https://www.openstreetmap.org/) (OSM) is a global, crowd-sourced
map of the world. earthlens ships a single `osm` backend that fetches OSM
features through **three public, keyless query protocols** and returns them
as a [pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection`
(a `geopandas.GeoDataFrame` subclass, CRS `EPSG:4326`):

| Protocol | SDK | What it answers | Geometry |
|---|---|---|---|
| **Overpass** | [`overpy`](https://github.com/DinoTools/python-overpy) | small/targeted **current-state** features by bbox + tag filter | points, lines, polygons |
| **ohsome** | [`ohsome`](https://github.com/GIScience/ohsome-py) | OSM **history + analytics** (features at a point in time / over a range) | points, lines, polygons |
| **pbf** | [`pyrosm`](https://pyrosm.readthedocs.io/) / [`pyosmium`](https://osmcode.org/pyosmium/) | **bulk / regional** reads from a Geofabrik `.osm.pbf` extract (a whole country's buildings, roads, …) | points, lines, polygons |

The first two are **live-query** protocols (small, targeted asks against a
shared public service). The `pbf` protocol is the **bulk** path: it downloads a
[Geofabrik](https://download.geofabrik.de/) regional extract once, caches it,
and reads a whole layer locally — the right tool for "every building in Malta",
which would blow past Overpass's size limits.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](osm.md) page.

## Why it matters here

Like the FDSN and GDACS backends, OSM departs from the gridded backends (CHC
rainfall, ERA5, GEE imagery) in two ways:

- **The output is a vector table, not a grid.** A query returns features — one
  row per OSM element, with `osm_id`, `osm_type`, the element's tags as columns,
  and a geometry. So OSM is a **`vector`** backend
  (`OSM.OUTPUT_KIND == "vector"`), and `download()` returns a
  `FeatureCollection`. Because there is no meaningful gridded reduction of a
  feature table, the `EarthLens` facade rejects an `aggregate=` argument for
  this backend with `NotImplementedError`.

- **There is no large dataset index to curate.** OSM is queried by tag filter,
  not chosen from an archive. The "catalog" is a small set of curated **named
  queries** (`overpass:hospitals`, `ohsome:buildings`, …) so you don't have to
  write raw Overpass QL or ohsome filters by hand — and a raw `query=` /
  `filter=` override is there when you do.

## The named queries

For this backend `variables` is the list of **named-query ids**, not
data-variable names (an intentional, documented overload — the `EarthLens`
facade makes `variables` required on every call). Each id is
`<protocol>:<name>`:

| Named query (`variables=[...]`) | Protocol | Returns |
|---|---|---|
| `overpass:hospitals` | Overpass | hospitals (points + footprints) |
| `overpass:roads` | Overpass | road / path centrelines (lines) |
| `overpass:buildings` | Overpass | building footprints (polygons) |
| `overpass:cafes` | Overpass | cafes (points) |
| `overpass:schools` | Overpass | schools (points + footprints) |
| `ohsome:buildings` | ohsome | building footprints at a snapshot/range |
| `ohsome:highways` | ohsome | road / path centrelines at a snapshot/range |
| `ohsome:amenities` | ohsome | tagged amenities at a snapshot/range |
| `pbf:buildings` | pbf | building footprints from a Geofabrik extract |
| `pbf:roads` | pbf | drivable road network from a Geofabrik extract |
| `pbf:pois` | pbf | points of interest from a Geofabrik extract |
| `pbf:landuse` | pbf | land-use polygons from a Geofabrik extract |
| `pbf:natural` | pbf | natural features from a Geofabrik extract |
| `pbf:boundaries` | pbf | administrative boundaries from a Geofabrik extract |

The `<protocol>:` prefix is what tells the backend which API to call. An unknown
id raises with a did-you-mean hint
(`Catalog().get("overpass:hospital")` → *Did you mean 'overpass:hospitals'?*).

A `pbf:*` query also needs a **`region=`** — a Geofabrik region key
(`"malta"`, `"netherlands"`, …, listed by `Catalog().region_ids()`) or a raw
Geofabrik path (`"europe/andorra"`). It picks which `.osm.pbf` extract to
download; the request bbox then clips the read.

## Authentication

**None.** Overpass, ohsome, and Geofabrik are all fully public — no key, no
token, no login, so there is no `authentication.md` page. The SDKs ship behind
two extras and are imported lazily, so the package imports fine without them:

- `pip install earthlens[osm]` → `overpy` + `ohsome` (the live protocols).
- `pip install earthlens[osm-pbf]` → `pyrosm` + `osmium` (the `pbf` protocol).
  Note `pyosmium` is published on PyPI as `osmium`. This extra is **not** part
  of `[all]` because `pyrosm` pulls the sdist-only `cykhash` (it would need a C
  compiler), so install it explicitly for bulk PBF work.

!!! note "Overpass needs a real User-Agent"
    The canonical `overpass-api.de` endpoint returns HTTP 406 to requests with
    no / a default `User-Agent`. The backend therefore POSTs the Overpass QL
    itself with a descriptive `User-Agent` (and parses the response with
    `overpy`), rather than using `overpy`'s built-in HTTP. The endpoint, the
    User-Agent, and the timeout are all constructor-overridable.

## What a query returns

One `FeatureCollection` (CRS `EPSG:4326`):

- **Overpass** — `osm_id`, `osm_type` (`node` / `way`), each element's OSM
  `tags` as columns, and a `geometry`: a `Point` for a node, a `LineString`
  for an open way, a `Polygon` for a closed way. Relations are skipped in the
  MVP.
- **ohsome** — the geometry plus ohsome's own columns, notably `@osmId` and
  `@snapshotTimestamp` (the history timestamp) and `@other_tags`.
- **pbf** — an `osm_id` / `osm_type` identity (pyrosm's native `id` column is
  normalised to `osm_id` so it matches the other paths) plus, with the default
  `pyrosm` engine, the layer's key tags (e.g. `building`); the `pyosmium`
  engine returns the slimmer `osm_id` / `osm_type` / `geometry` schema (see the
  engine note in [Usage](usage.md)).

As a side effect, `download()` also writes the collection to one vector file in
the output directory (GeoJSON by default, or GeoPackage).

## Licensing — ODbL share-alike

OSM data is published under the **[Open Database License (ODbL
1.0)](https://opendatacommons.org/licenses/odbl/)**, which is **share-alike**:
you must credit *"© OpenStreetMap contributors"* and license any *derived
database* you redistribute under ODbL. So **every** successful `download()`
emits a `LicenseWarning` naming the obligation — it is not optional metadata.
Honour it when you redistribute OSM-derived data.

## Bulk PBF lives in earthlens by design

The `pbf` reader wraps `pyrosm` / `pyosmium`. The pyramids porting policy would
normally push a *generic format reader* to pyramids — but `pyrosm` and
`pyosmium` are **OSM-domain SDKs**, not generic GIS libraries, so wrapping them
is exactly the per-provider-SDK role `earthlens.osm` already plays for
`overpy` / `ohsome`. By maintainer decision the whole OSM stack, PBF included,
stays in earthlens; it is **not** ported to pyramids.

The `pyrosm` (in-memory) engine reads a whole regional extract into memory and
is the default. For a **continent- or planet-scale** extract too large to hold
in memory, pass `engine="pyosmium"` to stream it with bounded memory; the
backend also warns before downloading a multi-GB extract and refuses to load a
>4 GB file with `pyrosm`. Never load `planet.osm` with `pyrosm`.

## Out of scope (follow-ons)

- **ohsome aggregation endpoints** (counts / areas / lengths over time). The
  MVP ships ohsome's `elements/geometry` *feature* path; the *aggregation* API
  is a separate follow-on (and is **not** earthlens `aggregate=`).

## Cost

**Free.** All three services are public infrastructure (Overpass mirrors; the
ohsome API run by HeiGIT; Geofabrik's extract server). Query gently: keep
Overpass / ohsome bboxes small and time ranges focused, and for `pbf` prefer
the smallest regional extract that covers your area (a country, not a
continent) and let the on-disk cache spare a re-download.

## References

- OpenStreetMap: <https://www.openstreetmap.org/>
- Overpass API: <https://wiki.openstreetmap.org/wiki/Overpass_API>
- ohsome API: <https://docs.ohsome.org/ohsome-api/v1/>
- Geofabrik extracts: <https://download.geofabrik.de/>
- pyrosm: <https://pyrosm.readthedocs.io/> · pyosmium: <https://osmcode.org/pyosmium/>
- ODbL: <https://opendatacommons.org/licenses/odbl/>
- earthlens OSM usage: [Usage](usage.md)
- earthlens OSM API: [Reference](osm.md)
