# OpenStreetMap features — introduction

[OpenStreetMap](https://www.openstreetmap.org/) (OSM) is a global, crowd-sourced
map of the world. earthlens ships a single `osm` backend that fetches OSM
features live through **two public, keyless query protocols** and returns them
as a [pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection`
(a `geopandas.GeoDataFrame` subclass, CRS `EPSG:4326`):

| Protocol | SDK | What it answers | Geometry |
|---|---|---|---|
| **Overpass** | [`overpy`](https://github.com/DinoTools/python-overpy) | small/targeted **current-state** features by bbox + tag filter | points, lines, polygons |
| **ohsome** | [`ohsome`](https://github.com/GIScience/ohsome-py) | OSM **history + analytics** (features at a point in time / over a range) | points, lines, polygons |

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

The `<protocol>:` prefix is what tells the backend which API to call. An unknown
id raises with a did-you-mean hint
(`Catalog().get("overpass:hospital")` → *Did you mean 'overpass:hospitals'?*).

## Authentication

**None.** Both Overpass and ohsome are fully public — no key, no token, no
login, so there is no `authentication.md` page. The two SDKs ship behind the
`[osm]` extra and are imported lazily (`pip install earthlens[osm]`); the
package imports fine without them.

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

As a side effect, `download()` also writes the collection to one vector file in
the output directory (GeoJSON by default, or GeoPackage).

## Licensing — ODbL share-alike

OSM data is published under the **[Open Database License (ODbL
1.0)](https://opendatacommons.org/licenses/odbl/)**, which is **share-alike**:
you must credit *"© OpenStreetMap contributors"* and license any *derived
database* you redistribute under ODbL. So **every** successful `download()`
emits a `LicenseWarning` naming the obligation — it is not optional metadata.
Honour it when you redistribute OSM-derived data.

## Out of scope (follow-ons)

- **Bulk OSM PBF / planet or region extracts.** The MVP is live, targeted
  Overpass / ohsome queries. A bulk PBF reader (the heavy planet-extract path)
  belongs in pyramids and is a follow-on, not part of this backend.
- **ohsome aggregation endpoints** (counts / areas / lengths over time). The
  MVP ships ohsome's `elements/geometry` *feature* path; the *aggregation* API
  is a separate follow-on (and is **not** earthlens `aggregate=`).

## Cost

**Free.** Both services are public, donation-funded community infrastructure
(Overpass mirrors; the ohsome API run by HeiGIT). Query gently: keep bboxes
small and time ranges focused, and respect each service's usage policy.

## References

- OpenStreetMap: <https://www.openstreetmap.org/>
- Overpass API: <https://wiki.openstreetmap.org/wiki/Overpass_API>
- ohsome API: <https://docs.ohsome.org/ohsome-api/v1/>
- ODbL: <https://opendatacommons.org/licenses/odbl/>
- earthlens OSM usage: [Usage](usage.md)
- earthlens OSM API: [Reference](osm.md)
