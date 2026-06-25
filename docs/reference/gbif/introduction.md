# GBIF species occurrences — introduction

The [GBIF](https://www.gbif.org/) (Global Biodiversity Information Facility)
occurrence API serves 3 B+ georeferenced species-occurrence records — every
"this species was observed at this place and time" point that GBIF's network
of museums, herbaria, citizen-science platforms, and monitoring programmes
publishes. earthlens ships a `gbif` backend that queries it through the
anonymous [`pygbif`](https://pygbif.readthedocs.io/) client.

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](gbif.md) page.

## What it returns

GBIF is a `vector` backend (`GBIF.OUTPUT_KIND == "vector"`): a query returns a
table of point features — one row per occurrence, each with a scientific
name, a taxon key, an event date, a dataset key, a license, and a `Point`
geometry. `download()` returns a
[pyramids](https://github.com/serapeum-org/pyramids) `FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) in `EPSG:4326` and, when `path` is set,
writes it (GeoParquet by default). Because there is no meaningful gridded
reduction of an occurrence table, the `EarthLens` facade rejects an
`aggregate=` argument.

## Selecting taxa

`variables` names the taxa to fetch, each resolved to a GBIF backbone
`taxonKey`:

- a **friendly key** from the bundled catalog (`"birds"` → 212, `"mammals"` →
  359, `"animals"` / `"plants"` / `"fungi"` → the kingdom roots);
- a **raw integer** `taxonKey` (`212`) or its digit string (`"212"`);
- a **name lookup** `"taxon:<scientific name>"` (e.g. `"taxon:Panthera leo"`),
  resolved live via `pygbif.species.name_backbone`.

An unknown friendly key raises with a did-you-mean hint.

## Pagination and the record cap

GBIF's search endpoint returns 300 records per page and rejects paging beyond
an `offset + limit` of 100,000. The backend loops pages up to `max_records`
(default 100,000) and logs a one-line note when the upstream count exceeds the
cap, pointing at GBIF's asynchronous download API for larger pulls. The bulk
download path (a Darwin Core Archive, which needs a free GBIF account) is a
deliberate follow-on; the MVP covers the anonymous paginated path.

## Licensing

GBIF normalises each record's license to `CC0_1_0`, `CC_BY_4_0`, or
`CC_BY_NC_4_0`. The backend raises a `LicenseWarning` when any record is
`CC_BY_NC_4_0`, so a downstream commercial user is told the non-commercial
obligation rides along rather than discovering it silently.
