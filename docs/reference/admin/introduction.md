# Administrative boundaries — introduction

earthlens ships a single `admin` backend that fetches **administrative-boundary
polygons** from four **public** sources and returns them as a pyramids
`FeatureCollection` of polygons in **EPSG:4326**. These are vector boundary
layers, not gridded rasters — so there is no meaningful grid reduction and
`aggregate=` is rejected.

Four sources are covered:

- **[geoBoundaries](https://www.geoboundaries.org/)** (gbOpen) — per-country
  administrative boundaries from **ADM0 to ADM5**, fetched by ISO3 country code.
  CC-BY-4.0. Two-step: the API returns metadata whose `gjDownloadURL` points at
  the GeoJSON.
- **[CGAZ](https://www.geoboundaries.org/globalDownloads.html)** (Comprehensive
  Global Admin Zones) — **single seamless global** layers at ADM0 / ADM1 / ADM2
  (one file, every country). CC-BY-4.0.
- **[Natural Earth](https://www.naturalearthdata.com/)** — global cultural admin
  layers (countries, states / provinces) at 10m / 50m / 110m scales. Public
  domain.
- **[US Census TIGER/Line](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)**
  — US states, counties, census tracts, and the nation outline, from the
  generalized cartographic-boundary (`cb_`) shapefiles. Public domain.

This page orients the backend. For the hands-on walkthrough see [Usage](usage.md)
and the shipped dataset ids on the [Available datasets](datasets.md) page; the
rendered API is the [Reference](admin.md) page.

## Why GADM is **not** here

[GADM](https://gadm.org/) is deliberately omitted. Its license forbids
commercial use and redistribution, which is incompatible with shipping the data
through an open tool. The four shipped sources are all CC-BY-4.0 or public
domain, so they can be fetched and redistributed freely (with attribution where
required). There is no plan to add GADM.

## Three design points

### Vector output; `aggregate=` rejected

`admin` declares `OUTPUT_KIND = "vector"`. `download()` returns a
`pyramids.feature.collection.FeatureCollection` of boundary polygons (and writes
it to one vector file when a `path` is set). The `EarthLens` facade reads
`OUTPUT_KIND` and rejects a non-`None` `aggregate=` with `NotImplementedError` —
boundaries are not a gridded field.

### No authentication

All four sources are public, so there is **no auth module** and **no extra
SDK**: the only dependencies are core `requests` and `pyramids`. On a successful
fetch the backend logs the per-source license (geoBoundaries / CGAZ CC-BY-4.0;
Natural Earth and TIGER public domain) so attribution is never lost.

### Uniform EPSG:4326 output

Sources arrive in different CRSs — TIGER in NAD83 (EPSG:4269), CGAZ in an
unlabelled geographic-degree CRS, the rest in EPSG:4326. The backend normalises
every result to **EPSG:4326**: a known non-4326 EPSG is reprojected, an
unlabelled-but-geographic CRS (CGAZ) is declared 4326 without a transform, and a
missing CRS is assumed WGS84. Every vector read goes through pyramids
`FeatureCollection.read_file` — earthlens never reads a vector file with a bare
`geopandas.read_file`.

## How it works

Each request names **one (or more) dataset id(s)** (via `variables=`) plus the
selector that dataset needs:

```python
from earthlens.core import EarthLens

# geoBoundaries ADM1 for Kenya -> a FeatureCollection of 47 county polygons
fc = EarthLens(
    data_source="admin",
    variables=["geoboundaries:adm1"],
    country="KEN",
).download()
```

The selector is per provider: `country=` (ISO3) for geoBoundaries, an optional
`scale=` for Natural Earth, an optional `year=` (and a `state=` FIPS code for the
per-state `tiger:tract`) for TIGER; CGAZ is seamless and needs none. A missing
required selector raises a clear `ValueError`.

## What is *not* here (yet)

An `bbox=` area-of-interest clip is a follow-on — the MVP returns the whole admin
layer for the selector. Point sampling and raster output are out of scope; this
is a pure vector backend.
