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
| Versions | `2.80` → `MSWEP_V280` (no `Hourly`), `3.16` → `MSWEP_V316_test` |

Hourly **NRT** granules carry an extra `model_id` field (1800 × 3600, values 1–18) identifying which predictor
stack produced each grid cell. At low and mid latitudes `model_id == 1` means the estimate is final and
consistent with the historical record; at high latitudes that is `model_id == 5`.

## MSWX

Ten meteorological variables at 0.1° / 3-hourly (`3hourly`, `Daily`, `Monthly` — no `Hourly`). All ten folder
spellings are confirmed against the share:

| Variable | Folder |
|---|---|
| 2-m air temperature | `Temp` |
| Precipitation | `P` |
| 2-m daily maximum air temperature | `Tmax` |
| 2-m daily minimum air temperature | `Tmin` |
| Surface pressure | `Pres` |
| 2-m relative humidity | `RelHum` |
| 2-m specific humidity | `SpecHum` |
| 10-m wind speed | `Wind` |
| Downward shortwave radiation | `SWd` |
| Downward longwave radiation | `LWd` |

## MSWX forecast streams

MSWX publishes two ensemble forecast streams alongside `Past` and `NRT`, as the folders **`Mid`** and **`Long`**.
They are catalogued but **not yet fetchable** — requesting one raises `NotImplementedError`:

| Stream | Folder | Base model | Members | Horizon | Re-initialised |
|---|---|---|---|---|---|
| Medium-range | `Mid` | NOAA GEFS | 30 | 10 days | daily |
| Seasonal (MSWX-Long) | `Long` | ECMWF SEAS5 | 51 | 7 months | monthly |

The blocker is structural, not a missing string. The confirmed layout is
`<variant>/<variable>/<YYYYMMDD_HH init>/<member NN>/<lead>.nc` — a forecast granule is keyed by an
initialisation time, an ensemble member (a **sub-folder**, `01` … `NN`) *and* a lead time, three coordinates the
analysis `path_template` cannot express. The archive is also sparse: historical inits carry only a few, empty
member folders, and `Long` was empty when surveyed. Implementing the fetch needs a forecast-aware path template
and is left as follow-up.

```python
from earthlens.mswep import Catalog

seasonal = Catalog().get_product("mswx").variants["Long"]
seasonal.members, seasonal.base_model, seasonal.horizon   # (51, 'SEAS5', '7 months')
```

## Where the folders live

`folder_id` **is** the version root. GloH2O shares one folder per product and per version — the id you are given
is the `MSWEP_V280` / `MSWX_V100` / `MSWEP_V316_test` folder itself, whose children are the variants. There is no
parent to search, so which version you get is decided by which share you point `folder_id` at; the `version=`
argument selects catalog metadata (units, the trend caveat), not the data.

Earlier releases of this backend marked unverified values `provisional` and refused them at request time; with
the share walked, every provisional flag has been dropped — the catalog now reflects the real layout.

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
