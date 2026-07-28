# Risk indicators — usage

The `risk-indicators` backend takes **one dataset id** and a **country selector**
and returns a `pandas.DataFrame` (tabular datasets) or a
`pyramids.feature.collection.FeatureCollection` (vector datasets). Reach it
through the `EarthLens` facade with `data_source="risk-indicators"` (aliases:
`"thinkhazard"`, `"inform"`, `"gfw"`, `"global-forest-watch"`).

## Selecting a dataset and a country

```python
from earthlens.core import EarthLens

df = EarthLens(
    data_source="risk-indicators",
    variables=["thinkhazard:flood_river"],   # exactly one dataset id
    country="KEN",                            # ISO3 country code
).download()
```

- `variables=` takes **exactly one** dataset id — the output kind is per
  instance, so a single call resolves to one shape. See
  [Available datasets](datasets.md) for the ids.
- `country=` is an **ISO3** code. For ThinkHazard you may instead pass a raw
  `admin_code=` (a ThinkHazard division code) to query a sub-national division.
- Spatial arguments (`lat_lim` / `lon_lim` / `aoi`) are accepted for signature
  parity but ignored — these datasets are country-indexed, not gridded.

## ThinkHazard! (public, tabular)

```python
# All 11 hazards for a country in one call
df = EarthLens(
    data_source="thinkhazard",
    variables=["thinkhazard:all"],
    country="KEN",
).download()
# columns: country, admin_code, hazard, hazard_type, level, level_title
```

The `country=` ISO3 is resolved to the ThinkHazard ADM0 division code (the FAO
GAUL 2015 ADM0 code) through a shipped lookup table. The `level` column carries
the mnemonic (`VLO` / `LOW` / `MED` / `HIG`) and `level_title` the word.

## INFORM Risk (public, tabular)

```python
df = EarthLens(
    data_source="inform",
    variables=["inform:risk"],     # or hazard_exposure / vulnerability / coping_capacity / climate_risk
    country="KEN",
).download()
# columns: iso3, indicator_id, indicator_score, validity_year
```

Omitting `country=` returns the score for **every** country in one frame.

## Global Forest Watch (needs a key)

GFW datasets need a free API key. Create one with a MyGFW account and pass it as
`api_key=` or set `GFW_API_KEY`:

```python
df = EarthLens(
    data_source="gfw",
    variables=["gfw:tree_cover_loss"],   # tabular: annual tree-cover loss (ha) by year
    country="KEN",
    api_key="<your-gfw-key>",            # or set GFW_API_KEY
).download()

fc = EarthLens(
    data_source="gfw",
    variables=["gfw:admin_boundary"],    # vector: the GADM admin polygon
    country="KEN",
).download()                              # -> a FeatureCollection
```

A GFW request with no key raises an `AuthenticationError` naming `GFW_API_KEY`.

### Creating a GFW API key

1. Create a **MyGFW** account at <https://www.globalforestwatch.org/> and confirm
   your email.
2. Mint a key via the Data API: `POST /auth/token` with your email + password to
   get a bearer token, then `POST /auth/apikey` (with that token) to create the
   key. See the
   [GFW guide](https://www.globalforestwatch.org/help/developers/guides/create-and-use-an-api-key/).
3. The key is sent on every request as the `x-api-key` header and **expires
   after ~1 year**. New keys can take a few minutes to become active.

## No aggregation

`aggregate=` is rejected for both tabular and vector datasets — these are
pre-computed indices and queries, so there is nothing to grid-reduce. Call
`download()` without it and post-process the returned `DataFrame` /
`FeatureCollection` directly.

## Why Aqueduct is not here

WRI Aqueduct's water-risk layers are mostly Google Earth Engine rasters; query
them through the `gee` backend's catalog instead of this one.
