# Risk indicators — introduction

<img src="../../_images/logos/risk_indicators.svg" alt="ThinkHazard! (GFDRR/World Bank) logo" height="60">

earthlens ships a single `risk-indicators` backend that fetches **country /
admin-indexed risk indicators** from three open or keyed sources and returns
them either as a tidy `pandas.DataFrame` or, for geometry, a pyramids
`FeatureCollection`. These are **pre-computed indices and queries keyed by
country** (an ISO3 code or an admin division), not gridded rasters — so there is
no bounding box and no grid, and `aggregate=` is rejected.

Three sources are covered:

- **[ThinkHazard!](https://thinkhazard.org)** (GFDRR) — natural-hazard screening
  across **11 hazards** (river / urban / coastal flood, earthquake, landslide,
  tsunami, cyclone, water scarcity, extreme heat, wildfire, volcano), by admin
  division. Public, no credentials. Tabular.
- **[INFORM Risk](https://drmkc.jrc.ec.europa.eu/inform-index)** (European
  Commission JRC) — the composite country-risk index and its three dimensions
  (Hazard & Exposure, Vulnerability, Lack of Coping Capacity), plus a climate
  variant. Public, no credentials. Tabular.
- **[Global Forest Watch](https://www.globalforestwatch.org)** Data API — forest
  indicators (annual tree-cover loss by country) and the GADM admin-boundary
  geometry. **Needs a free API key** (`GFW_API_KEY`). Tabular for the loss
  table, vector for the geometry.

This page orients the backend. For the hands-on walkthrough see [Usage](usage.md)
and the shipped dataset ids on the [Available datasets](datasets.md) page; the
rendered API is the [Reference](risk_indicators.md) page.

## Two design points

### Per-instance output kind

The output shape is **decided per request** by the resolved dataset's
`output_kind`: a `tabular` dataset returns a `pandas.DataFrame`, a `vector`
dataset returns a `pyramids.feature.collection.FeatureCollection`. The
`EarthLens` facade reads this to know the return shape and to reject
`aggregate=` (there is nothing to grid-reduce on a pre-computed index).

### Per-source authentication

Authentication is **not** backend-global. ThinkHazard! and INFORM are public, so
a request for one of their datasets builds no auth at all. Only a dataset whose
provider is `gfw` constructs a `GfwAuth` and requires a key — passed as
`api_key=` or read from the `GFW_API_KEY` environment variable. A GFW request
with no key fails fast with an `AuthenticationError` naming `GFW_API_KEY`.

## How it works

Each request names **one dataset id** (via `variables=`) plus a country
selector (`country="KEN"` ISO3, or a raw `admin_code=`). The backend resolves
the dataset's catalog row, routes to its provider, issues the keyed-or-public
REST call, and parses the JSON into the right shape:

```python
from earthlens.core import EarthLens

# ThinkHazard! river-flood level for Kenya -> a one-hazard DataFrame
df = EarthLens(
    data_source="risk-indicators",
    variables=["thinkhazard:flood_river"],
    country="KEN",
).download()
```

For ThinkHazard, the `country=` ISO3 is resolved to the ThinkHazard ADM0
division code (the FAO GAUL 2015 code) via a shipped lookup table; a raw
`admin_code=` is also accepted for sub-national divisions.

## What is *not* here

WRI **Aqueduct** water-risk layers are out of scope for this backend: they are
mostly Google Earth Engine rasters and belong in the `gee` backend's catalog,
not here. GFW raster tile endpoints are a follow-on; this backend ships the SQL
(table) and geometry (vector) query paths.
