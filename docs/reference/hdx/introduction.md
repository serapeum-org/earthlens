# Humanitarian Data Exchange (HDX) — introduction

`earthlens.hdx` is one wrapper over the **CKAN API of UN OCHA's
[Humanitarian Data Exchange](https://data.humdata.org)** (~21,000
datasets) via the official read-only `hdx-python-api` SDK. A request
names a curated **dataset** plus an optional **resource filter**; the
backend resolves the dataset, filters its resources, and downloads the
matching files to disk.

## What it covers

HDX aggregates humanitarian and geospatial data from hundreds of
organisations. earthlens ships a curated catalogue across these
families (and an `hdx_id=` escape hatch for any of the ~21k datasets):

- **Population & settlement** — Kontur Population (global 400 m H3
  hexagons), Meta High Resolution Settlement Layer (HRSL), WorldPop
  population counts.
- **Administrative boundaries** — Kontur boundaries and OCHA Common
  Operational Datasets (COD-AB).
- **OpenStreetMap exports** — HOTOSM buildings / roads / waterways /
  points of interest per country.
- **Socioeconomic indicators** — UNDP Human Development Index and
  related indices.
- **Forced displacement** — UNHCR refugee statistics.
- **Food security** — WFP food prices and food-security indicators.
- **Conflict** — ACLED political-violence event tables.

## Output kind — the first `mixed` backend

An HDX resource is whatever the contributor uploaded — CSV, GeoTIFF,
GeoPackage, Shapefile, GeoJSON, Parquet — and one dataset can carry
several kinds at once. So `HDX.OUTPUT_KIND = "mixed"`, the first mixed
backend in earthlens. The MVP **downloads each resource file as-is** and
records its CKAN format label; reading / converting a resource into a
pyramids data type is a deferred follow-on (`PY-D`), not done here.

Because the result is files-as-is, the aggregator does not apply: the
`EarthLens` facade forwards `aggregate=` for a `mixed` backend, but
`HDX.download(aggregate=...)` rejects a non-`None` value with
`NotImplementedError`.

## How it maps onto the `EarthLens` facade

`HDX` is registered under the `"hdx"` key:

```python
from earthlens.earthlens import EarthLens

paths = EarthLens(
    data_source="hdx",
    variables={"kontur-population": []},
    path="data/hdx",
).download()
```

HDX/CKAN has **no spatial or temporal query** — datasets are addressed
by id. The facade still requires a bounding box and accepts a date
range, but for HDX these are **ignored for the query** (at most recorded
as metadata). See [Usage](usage.md) for the request shape.

## Install

HDX is public — there are **no credentials**. Install the optional
extra:

```bash
pip install earthlens[hdx]
```

This pulls `hdx-python-api>=6,<7`. The SDK import is lazy, so earthlens
imports fine without the extra; a missing extra surfaces as a friendly
`ImportError` naming `earthlens[hdx]`.
