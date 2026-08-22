# Risk indicators — available datasets

The `risk-indicators` catalog ships the dataset ids below across the three
providers. Pass one as the single entry in `variables=`. The `output_kind`
column tells you the return shape: `tabular` → a `pandas.DataFrame`, `vector` →
a `pyramids.feature.collection.FeatureCollection`.

List them live at any time:

```python
from earthlens.core import EarthLens

EarthLens.list_datasets("risk-indicators")
```

## ThinkHazard! (public, tabular)

Natural-hazard screening across 11 hazards. Returns one row per hazard with
`country`, `admin_code`, `hazard`, `hazard_type`, `level` (mnemonic) and
`level_title`. Needs `country=` (ISO3) or a raw `admin_code=`.

| dataset id | output kind | description |
|---|---|---|
| `thinkhazard:flood_river` | tabular | River flood hazard level |
| `thinkhazard:flood_urban` | tabular | Urban flood hazard level |
| `thinkhazard:flood_coastal` | tabular | Coastal flood hazard level |
| `thinkhazard:earthquake` | tabular | Earthquake hazard level |
| `thinkhazard:landslide` | tabular | Landslide hazard level |
| `thinkhazard:tsunami` | tabular | Tsunami hazard level |
| `thinkhazard:cyclone` | tabular | Cyclone hazard level |
| `thinkhazard:water_scarcity` | tabular | Water scarcity (drought) hazard level |
| `thinkhazard:extreme_heat` | tabular | Extreme heat hazard level |
| `thinkhazard:wildfire` | tabular | Wildfire hazard level |
| `thinkhazard:volcano` | tabular | Volcano hazard level |
| `thinkhazard:all` | tabular | All 11 hazard levels for a division |

## INFORM Risk (public, tabular)

The composite index and its three dimensions, plus the climate variant. Returns
`iso3`, `indicator_id`, `indicator_score`, `validity_year`, `workflow_id` and
`source`. The four Risk datasets read JRC's published release workbook by default
(the current release) and the Scores API when asked — see
[which release you get](usage.md#which-release-you-get) — and every row records
which channel served it. `country=` filters to one country; omit it for every
country.

| dataset id | output kind | description |
|---|---|---|
| `inform:risk` | tabular | INFORM Risk composite index |
| `inform:hazard_exposure` | tabular | Hazard & Exposure dimension |
| `inform:vulnerability` | tabular | Vulnerability dimension |
| `inform:coping_capacity` | tabular | Lack of Coping Capacity dimension |
| `inform:climate_risk` | tabular | INFORM Climate Change Risk 2050 (RCP4.5-SSP1) |

`inform:climate_risk` reads INFORM's separate Climate Change model, not the Risk release the
other four rows come from. INFORM publishes two pathways — "optimistic" (RCP4.5-SSP1) and
"pessimistic" (RCP8.5-SSP3), at 2050 and 2080 — but its API serves scores for only the
optimistic 2050 run, so that is the one this dataset returns. The pessimistic runs are
available from the [Climate Change results
page](https://drmkc.jrc.ec.europa.eu/inform-index/INFORM-Climate-Change/Results-and-data)
as a spreadsheet.

## Global Forest Watch (needs `GFW_API_KEY`)

Forest indicators and the GADM admin geometry. Needs `country=` (ISO3) and a key.

| dataset id | output kind | description |
|---|---|---|
| `gfw:tree_cover_loss` | tabular | Annual tree-cover loss (ha) by year (UMD/Hansen, canopy ≥ 30%) |
| `gfw:tree_cover_loss_summary` | tabular | Total tree-cover loss (ha) (UMD/Hansen, canopy ≥ 30%) |
| `gfw:admin_boundary` | vector | GADM admin-boundary geometry the indicators are computed over |

## Country / admin resolution

ThinkHazard keys its API on numeric division codes, which at country level are
the **FAO GAUL 2015 ADM0 codes**. The catalog ships an ISO3 → code lookup
(243 countries) so `country="KEN"` resolves to division `133`. A raw
`admin_code=` bypasses the lookup for a sub-national division.
