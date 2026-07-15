# OBIS marine occurrences — introduction

<img src="../../_images/logos/obis.png" alt="OBIS logo" height="60">

The [OBIS](https://obis.org/) (Ocean Biodiversity Information System)
occurrence API serves 140 M+ georeferenced **marine** species-occurrence
records — the ocean counterpart to GBIF. earthlens ships an `obis` backend
that queries it through the anonymous [`pyobis`](https://github.com/iobis/pyobis)
client.

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](obis.md) page.

## The marine twin of GBIF

OBIS is built as the marine twin of the [GBIF](../gbif/introduction.md)
backend: same `vector` output, the same shared `occurrences_to_fc` mapper, and
the same return contract — a points `FeatureCollection` in `EPSG:4326`, one
row per occurrence, written as GeoParquet by default. Only the SDK call and
the request fields differ.

One detail is worth knowing: `pyobis` 1.x returns a lazy `OccResponse` query
object whose `.execute()` yields a `pandas.DataFrame` (one row per record) —
not the older `{"results": [...]}` dict. The backend consumes that DataFrame
directly. OBIS is Darwin Core, so its coordinate fields are
`decimalLatitude` / `decimalLongitude`, the same as GBIF.

## Selecting species

`variables` names the species to fetch, each resolved to an OBIS
`scientificname`:

- a **friendly key** from the bundled catalog (`"blue-whale"`,
  `"common-dolphin"`, `"ocean-sunfish"`, …);
- an explicit `"species:<scientific name>"` selector (e.g.
  `"species:Mola mola"`), passed through verbatim.

An unknown friendly key raises with a did-you-mean hint.

## Licensing

OBIS data is mostly `CC-BY-4.0`, but some datasets are `CC0` or `CC-BY-NC`.
The backend raises a `LicenseWarning` when any record's license is
non-commercial (`CC-BY-NC`).
