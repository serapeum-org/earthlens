# Biodiversity cluster — introduction

earthlens ships four **biodiversity / conservation** backends that share one
request shape — a taxon / area selector over a bounding box (and, for
occurrences, a time window) — and one small set of helpers:

| Backend | Source | Key(s) | Output | Auth |
|---|---|---|---|---|
| [`gbif`](../gbif/introduction.md) | GBIF — 3 B+ species occurrences | `gbif` | `vector` (occurrence points) | none |
| [`obis`](../obis/introduction.md) | OBIS — 140 M+ marine occurrences | `obis` | `vector` (occurrence points) | none |
| [`wdpa`](../wdpa/introduction.md) | Protected Planet — 300 k+ protected areas | `wdpa` / `protected-planet` | `vector` (protected-area polygons) | token (`WDPA_TOKEN`) |
| [`iucn`](../iucn/introduction.md) | IUCN Red List — assessments | `iucn` / `redlist` | `tabular` (assessments) | token (`IUCN_TOKEN`) |

They are **four independent backends**, not one merged source — their SDKs,
auth, and output kinds differ enough that four small subpackages read more
clearly than a single registry backend. What they share lives in a tiny
helper package, `earthlens.biodiversity`.

## The shared shape

GBIF and OBIS are near-identical: an occurrence search over a geometry + time
+ taxon returns a points `GeoDataFrame`. WDPA returns protected-area polygons.
IUCN returns tabular assessment records. The first three are `vector`
backends whose `download()` returns a
[pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection`
(a `geopandas.GeoDataFrame` subclass) in `EPSG:4326`; IUCN is the cluster's
only `tabular` backend, returning a `pandas.DataFrame`. Because none of them
is gridded, the `EarthLens` facade rejects an `aggregate=` argument for all
four with `NotImplementedError`.

Three pieces are factored into `earthlens.biodiversity`:

- **`wkt_from_bbox`** turns a `SpatialExtent` (which exposes
  `.west/.south/.east/.north` but no `.wkt()`) into the counter-clockwise
  `POLYGON((...))` WKT the occurrence/area APIs accept as a `geometry=`
  filter.
- **`occurrences_to_fc`** maps occurrence rows — a `list[dict]` from `pygbif`
  or the `pandas.DataFrame` `pyobis` returns — into a points
  `FeatureCollection`, modelled on `earthlens.fdsn.events`. A row with a
  missing coordinate gets a null geometry rather than an invalid
  `POINT (nan nan)`; an empty result keeps the column schema.
- **`LicenseWarning` / `warn_license`** flag results that carry
  attribution, non-commercial, or restricted-redistribution obligations.

## License policy

Biodiversity data licenses vary, so every backend warns where the license is
restrictive:

- **GBIF** — per-record licenses are `CC0` / `CC-BY` / `CC-BY-NC`; the backend
  warns when any record is `CC_BY_NC_4_0`.
- **OBIS** — mostly `CC-BY-4.0`, but the backend warns on any `CC-BY-NC` row.
- **WDPA** — a custom UNEP-WCMC license (commercial use needs written
  permission, redistribution is restricted): **every** download warns.
- **IUCN** — `CC-BY-NC`, and redistribution needs a written IUCN waiver:
  **every** download warns, and Red List data is never shipped as package
  data — the token stays user-supplied.

`LicenseWarning` is a single shared class (promoted here from the Overture
backend), so a downstream consumer can filter every backend's license
warnings with one `warnings` filter.
