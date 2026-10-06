# OpenStreetMap features — introduction

<img src="../../_images/logos/osm.svg" alt="OpenStreetMap logo" height="60">

[OpenStreetMap](https://www.openstreetmap.org/) (OSM) is a global, crowd-sourced
map of the world. earthlens ships a single `osm` backend that fetches OSM
features through **three public, keyless download types** and returns them as a
`FeatureCollection` (a `geopandas.GeoDataFrame` subclass, CRS `EPSG:4326`):

| Download type | What it answers | Geometry |
|---|---|---|
| **`live:`** | small/targeted **current-state** features by bbox + tag filter | points, lines, polygons |
| **`history:`** | OSM **history** — features at a point in time or over a range | points, lines, polygons |
| **`bulk:`** | **bulk / regional** reads of a whole area (every building in a country, …) | points, lines, polygons |

The first two are **live queries** (small, targeted asks). The `pbf` type is the
**bulk** path: it downloads a regional extract once, caches it, and reads a whole
layer locally — the right tool for "every building in Malta", which would blow
past a live query's size limits.

This page orients the backend. For the hands-on download walkthrough see
[Usage](usage.md); the rendered API is the [Reference](osm.md) page.

## Install

The `osm` backend lives in the **`earthlens-hazards`** provider distribution. Its
`osm` extra adds what the three download types need; install it in whichever way
fits how much of earthlens you want:

| You want… | Install |
|---|---|
| **Just OSM, leanest** — the hazards provider + core only | `pip install earthlens-hazards[osm]` |
| **OSM via the umbrella package** — all providers' code, OSM switched on | `pip install earthlens[osm]` |
| **Everything, every SDK** | `pip install earthlens[all]` |
| **All providers' code, no optional SDKs** (add extras later) | `pip install earthlens` |
| **The hazards provider's code, no optional SDKs** | `pip install earthlens-hazards` |
| **The opt-in in-memory pbf engine**, on top of any of the above | `pip install pyrosm` |

- **`earthlens-hazards[osm]` is the smallest install that runs the backend** — it
  pulls `earthlens-core` + `earthlens-hazards` and the `osm` extra, and none of
  the other four providers (atmosphere / ocean / imagery / land).
- **`earthlens[osm]` is that same `osm` extra re-exported by the umbrella
  `earthlens` package** (`earthlens[osm]` → `earthlens-hazards[osm]`). The
  umbrella pulls *every* provider's code as its base, so it is the heavier
  install — use it when you want the whole toolkit with OSM switched on.
- **There is no `earthlens[hazards]` extra.** Extras are per-backend (`[osm]`,
  `[fdsn]`, `[overture]`, …) or the catch-all `[all]`; to get the whole hazards
  *provider* (every hazards backend, SDKs off) install the distribution itself:
  `pip install earthlens-hazards`.
- Plain `pip install earthlens` / `earthlens-hazards` installs the backend's
  *code* but not its extra, so an OSM download raises a clear `ImportError` until
  you add `[osm]`.
- No credentials to configure — every download type is public and keyless.

## The three download types

The `osm` backend is really three different ways to get OSM data, chosen by the
`<type>:` prefix of the named query. They answer different questions and have
very different freshness and cost characteristics, so picking the right one
matters more than with a single-source backend.

### `live:` — current-state queries

Named queries: `live:hospitals` / `roads` / `buildings` / `cafes` /
`schools`.

A live query that returns **what the map says right now** — there is no time
axis. You give a bbox and a named query (a tag filter); the backend runs the
query and hands back the matching features.

Reach for it when you want a **small, targeted, up-to-the-minute** slice — "the
hospitals in this neighbourhood", "the café points in this bbox". It is the
freshest source, but it runs against **shared public infrastructure with hard
usage limits**: keep the bbox small (earthlens rejects a box larger than
`max_bbox_deg2`, default 100 deg²), expect throttling if you burst, and pause
between queries. It has no history, and relations are skipped in the MVP. When
the named queries are not enough, pass a raw query through `query=`. The request
endpoint, User-Agent, and timeout are all overridable (`endpoint=`,
`user_agent=`, `timeout=`).

### `history:` — history and change over time

Named queries: `history:buildings` / `highways` / `amenities`.

Unlike `live:`, this type knows the **full edit history** of every OSM
element, so it answers **temporal questions**: what a feature looked like at a
past instant, or how an area was mapped across a span of time. A single date
(`start=`) returns one **snapshot**; `start=` + `end=` returns the **range**
`start/end` — each feature at both boundary snapshots, carried in the
`@snapshotTimestamp` column. An `history:` query therefore **requires a time** —
it raises without `start=`.

Reach for it for change detection and "as-of" maps — "buildings as they existed
on 2018-01-01", "how coverage grew from 2015 to 2023". It too is rate-limited
public infrastructure (a transient throttle is retried automatically; a hard
block surfaces as a clear, typed error). A raw filter goes through `filter=`.
The `history:` aggregation queries (counts / areas / lengths over time) are out
of scope for now.

### `bulk:` — regional, offline reads

Named queries: `bulk:buildings` / `roads` / `pois` / `landuse` / `natural` /
`boundaries`.

`pbf` is **not a live query**. It reads a regional `.osm.pbf` extract — a
compressed snapshot of a whole area's OSM data — which the backend downloads
once, caches on disk, and then reads **locally**. Because the work is local it
scales to asks that would blow past a live query's limits outright: *every*
building in a country, a whole national road network, or the same area read
repeatedly without touching a shared service.

A `bulk:` query needs a **`region=`** — a region key such as `"malta"` (listed by
`Catalog().region_ids()`) or a raw `"continent/region"` path such as
`"europe/monaco"` — which selects the extract; the request bbox then clips the
read (omit it to read the whole extract). Two read **engines** trade memory
against richness: the default **`engine="pyosmium"`** streams with bounded memory
(slim `osm_id` / `osm_type` / `geometry` schema, scales to a continent or the
whole planet) and is installed with the backend; the opt-in
**`engine="pyrosm"`** reads the whole extract into memory for the richest, exact
per-layer columns and refuses a file over 4 GB. The first call pays the
download; later calls reuse the cache.

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
  queries** (`live:hospitals`, `history:buildings`, …) so you don't have to
  write raw Overpass QL or ohsome filters by hand — and a raw `query=` /
  `filter=` override is there when you do.

## The named queries

For this backend `variables` is the list of **named-query ids**, not
data-variable names (an intentional, documented overload — the `EarthLens`
facade makes `variables` required on every call). Each id is `<type>:<name>`:

| Named query (`variables=[...]`) | Type | Returns |
|---|---|---|
| `live:hospitals` | `live:` | hospitals (points + footprints) |
| `live:roads` | `live:` | road / path centrelines (lines) |
| `live:buildings` | `live:` | building footprints (polygons) |
| `live:cafes` | `live:` | cafes (points) |
| `live:schools` | `live:` | schools (points + footprints) |
| `history:buildings` | `history:` | building footprints at a snapshot/range |
| `history:highways` | `history:` | road / path centrelines at a snapshot/range |
| `history:amenities` | `history:` | tagged amenities at a snapshot/range |
| `bulk:buildings` | `bulk:` | building footprints from a regional extract |
| `bulk:roads` | `bulk:` | drivable road network from a regional extract |
| `bulk:pois` | `bulk:` | points of interest from a regional extract |
| `bulk:landuse` | `bulk:` | land-use polygons from a regional extract |
| `bulk:natural` | `bulk:` | natural features from a regional extract |
| `bulk:boundaries` | `bulk:` | administrative boundaries from a regional extract |

The `<type>:` prefix is what tells the backend which download path to take. An
unknown id raises with a did-you-mean hint
(`Catalog().get("live:hospital")` → *Did you mean 'live:hospitals'?*).

The canonical names map to the underlying tools: `live` is the Overpass API,
`history` is ohsome, `bulk` is a `.osm.pbf` extract. The original prefixes still
work as **back-compat aliases** — `overpass:hospitals` is accepted as
`live:hospitals`, `ohsome:…` as `history:…`, and `pbf:…` as `bulk:…` — and
`overpass` / `ohsome` also work as a facade `data_source`.
`list_datasets("osm")` always lists the canonical names.

A `bulk:*` query also needs a **`region=`** — a region key (`"malta"`,
`"netherlands"`, …, listed by `Catalog().region_ids()`) or a raw
`"continent/region"` path (`"europe/andorra"`). It picks which extract to
download; the request bbox then clips the read.

## What a query returns

One `FeatureCollection` (CRS `EPSG:4326`):

- **`live:`** — `osm_id`, `osm_type` (`node` / `way`), each element's OSM
  tags as columns, and a `geometry`: a `Point` for a node, a `LineString` for an
  open way, a `Polygon` for a closed way. Relations are skipped in the MVP.
- **`history:`** — the geometry plus the history columns, notably `@osmId` and
  `@snapshotTimestamp` (the snapshot instant) and `@other_tags`.
- **`bulk:`** — an `osm_id` / `osm_type` identity (normalised so it matches the
  other types). The default `engine="pyosmium"` returns the slim `osm_id` /
  `osm_type` / `geometry` schema; the opt-in `engine="pyrosm"` adds the layer's
  key tags (e.g. `building`) and exact per-layer columns.

As a side effect, `download()` also writes the collection to one vector file in
the output directory (GeoJSON by default, or GeoPackage via `file_format=`).

## Licensing — ODbL share-alike

OSM data is published under the **[Open Database License (ODbL
1.0)](https://opendatacommons.org/licenses/odbl/)**, which is **share-alike**:
you must credit *"© OpenStreetMap contributors"* and license any *derived
database* you redistribute under ODbL. So **every** successful `download()`
emits a `LicenseWarning` naming the obligation — it is not optional metadata.
Honour it when you redistribute OSM-derived data.

## Cost

**Free.** All three download types use public, keyless infrastructure. Query
gently: keep the live-query bboxes small and time ranges focused, and for `pbf`
prefer the smallest regional extract that covers your area (a country, not a
continent) and let the on-disk cache spare a re-download.

## References

- OpenStreetMap: <https://www.openstreetmap.org/>
- ODbL: <https://opendatacommons.org/licenses/odbl/>
- earthlens OSM usage: [Usage](usage.md)
- earthlens OSM API: [Reference](osm.md)
