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
    variables=["inform:risk"],     # or hazard_exposure / vulnerability / coping_capacity
    country="KEN",
).download()
# columns: iso3, indicator_id, indicator_score, validity_year, workflow_id, source
```

Omitting `country=` returns the score for **every** country in one frame.

### Which release you get

JRC publishes INFORM Risk through two channels, and they do not always agree:

- the **release workbook** on the [results
  page](https://drmkc.jrc.ec.europa.eu/inform-index/INFORM-Risk/Results-and-data) — the
  current release (2026 at the time of writing);
- the **Scores API**, which serves one *workflow* (model release) per request.

By default the four Risk datasets read the **workbook**, so you get the release
JRC currently publishes. The workbook is downloaded once and cached (under the
shared earthlens cache, or `cache_dir=`), then reused by all four datasets.

`source=` chooses explicitly, and `workflow_id=` implies the API:

```python
current = EarthLens(                       # the published release (default)
    data_source="inform", variables=["inform:risk"], country="KEN",
).download()

pinned = EarthLens(                        # the API's pinned workflow, 503
    data_source="inform", variables=["inform:risk"], country="KEN", source="api",
).download()

older = EarthLens(                         # a specific model release
    data_source="inform", variables=["inform:risk"], country="KEN", workflow_id=493,
).download()
```

Every row records where it came from: `source` is `release` or `api`,
`workflow_id` is the workflow an API row was fetched with (empty for a workbook
row), and `validity_year` carries the workbook's release year (the API leaves it
at `0`). The values differ between channels — Kenya scores 6.2 in the 2026
workbook and 5.8 under workflow 503 — so that provenance matters when comparing
tables.

Why both: the API stopped serving the 2026 workflows on 2026-08-18 while the
results page kept publishing the 2026 release, so the workbook is the reliable
route to the current release and the API is the route to any other one. When a
source serves nothing, the empty result says which one it was rather than
writing an unexplained empty table.

!!! warning "The four Risk datasets changed what they return"
    They previously answered from the Scores API. They now read the release
    workbook by default, so the **values change** (Kenya 5.8 → 6.2), the frame
    gains a `source` column, `validity_year` carries a real year instead of `0`,
    and `workflow_id` is empty on a workbook row. Pass `source="api"` to get the
    previous behaviour, and compare tables by their `source` rather than
    assuming two files came from the same channel.

`inform:climate_risk` reads a different model — INFORM's Climate Change
projection for 2050 under the optimistic RCP4.5-SSP1 pathway (workflow `451`) —
so it is not a drop-in swap for the four Risk datasets above and its scores are
not comparable with theirs. It is published separately from the Risk workbook,
so it is API-only: `source="release"` is rejected for it. See the
[dataset list](datasets.md).

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
