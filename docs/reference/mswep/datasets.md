# MSWEP / MSWX — products and catalog

The bundled `mswep_data_catalog.yaml` carries everything needed to build a Drive path. Inspect it with no
network and no credentials:

```python
from earthlens.mswep import Catalog

cat = Catalog()
cat.products()                                  # ['mswep', 'mswx']
cat.get_product("mswep").variants["Past"].end   # datetime.date(2024, 12, 31)
cat.get_product("mswx").path_template           # includes {variable}
```

## MSWEP

| | |
|---|---|
| Variable | `precipitation` (1800 × 3600, 0.1° global) |
| Units | `mm/hour`, `mm/3-hour`, `mm/day`, `mm/month` by resolution |
| Resolutions | `Hourly`, `3hourly`, `Daily`, `Monthly` |
| Variants | `Past` (gauge-corrected), `Past_nogauge` (satellite-reanalysis baseline), `NRT` |
| Versions | `2.80` → `MSWEP_V280`, `3.15` → `MSWEP_V315` |

Hourly **NRT** granules carry an extra `model_id` field (1800 × 3600, values 1–18) identifying which predictor
stack produced each grid cell. At low and mid latitudes `model_id == 1` means the estimate is final and
consistent with the historical record; at high latitudes that is `model_id == 5`.

## MSWX

Ten meteorological variables at 0.1° / 3-hourly. `Temp` is the only folder spelling externally confirmed; the
rest are `provisional` in the catalog and refused until verified against a real share.

| Variable | Folder | Confirmed |
|---|---|---|
| 2-m air temperature | `Temp` | yes |
| Precipitation | `P` | no |
| 2-m daily maximum air temperature | `Tmax` | no |
| 2-m daily minimum air temperature | `Tmin` | no |
| Surface pressure | `Pres` | no |
| 2-m relative humidity | `RelHum` | no |
| 2-m specific humidity | `SpecHum` | no |
| 10-m wind speed | `Wind` | no |
| Downward shortwave radiation | `SWd` | no |
| Downward longwave radiation | `LWd` | no |

## MSWX forecast streams

MSWX publishes two ensemble forecast streams alongside `Past` and `NRT`. They are catalogued with everything
that is publicly documented, but **cannot be downloaded yet**:

| Stream | Base model | Members | Horizon | Re-initialised |
|---|---|---|---|---|
| `Medium Range Forecast` | NOAA GEFS | 30 | 10 days | daily |
| `Seasonal Forecast` (MSWX-Long) | ECMWF SEAS5 | 51 | 7 months | monthly |

Requesting one raises `NotImplementedError` explaining why, rather than silently returning nothing.

The blocker is structural, not a missing string. An analysis granule is addressed by
`<root>/<variant>/<variable>/<temporal>/<valid-time>.nc`, but a **forecast** granule is identified by an
initialisation time, a lead time *and* an ensemble member — three coordinates that template does not carry. It
is also unpublished whether members are sub-folders or a file-name component, and whether the stem encodes the
initialisation time, the valid time, or both.

Pinning that layout needs an approved share, and is part of task `A1`. Once known, the catalog needs a
forecast-aware path template rather than a new row.

```python
from earthlens.mswep import Catalog

seasonal = Catalog().get_product("mswx").variants["Seasonal Forecast"]
seasonal.members, seasonal.base_model, seasonal.horizon   # (51, 'SEAS5', '7 months')
print(seasonal.notes)                                     # exactly what is unpinned
```

## Provisional values

A row marked `provisional: true` could not be verified without an approved share. `Catalog.check_not_provisional`
refuses it at request time, because resolving against a guess would build a Drive path that does not exist — and
since a missing granule is logged and skipped, that would return a **silently partial** time series rather than
an error.

Still provisional: the live V3.16 root folder name, nine MSWX variable folders, MSWX's `NRT` window and its
`3hourly` folder.

## Gauge metadata

The share carries a `Gauge_metadata/` folder of auxiliary CSVs describing the rain gauges behind MSWEP's
gauge-correction step. Fetch them with `fetch_gauge_metadata()` — a separate method, because they are static
reference data rather than a time series, so pushing them through `download()`'s date window would be
meaningless:

```python
from earthlens.mswep import MSWEP

src = MSWEP(start="2020-04-25", end="2020-04-25", temporal_resolution="daily", path="out")
paths = src.fetch_gauge_metadata()            # all five
paths = src.fetch_gauge_metadata(["daily_station_locations.csv"])   # or a subset
```

They land under `out/Gauge_metadata/` and are shipped raw — read them with pandas.

| File | Contents |
|---|---|
| `daily_station_locations.csv` | Lat/lon per daily gauge that passed QC and deduplication. Ids are source station codes (`GHCND_GME00010350`). |
| `monthly_station_locations.csv` | Lat/lon per monthly gauge. Ids (`gridcell_00042230`) are 0.25° GPCC cells; the suffix is the flattened index in a 720×1440 grid, row-major from top-left. |
| `daily_station_date_ranges.csv` | First/last valid observation per daily gauge. |
| `monthly_station_date_ranges.csv` | Same, for monthly gauges. |
| `daily_station_reporting_times.csv` | Inferred reporting time as an hour offset from 00:00 UTC. `-5` means totals likely end 19:00 UTC; `0` aligns with the UTC day; `+3` means 03:00 UTC next day. |

!!! warning "A date range is not continuous coverage"
    The range files bound the temporal span only. Many gauges have gaps — missing days, months or whole years.

!!! note "Where the folder lives"
    The documentation names the folder and every file but not its **parent**. The backend therefore probes the
    version root (alongside `Past` / `NRT`) first, then the share root, and reports both if neither has it.
