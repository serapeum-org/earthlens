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

## Provisional values

A row marked `provisional: true` could not be verified without an approved share. `Catalog.check_not_provisional`
refuses it at request time, because resolving against a guess would build a Drive path that does not exist — and
since a missing granule is logged and skipped, that would return a **silently partial** time series rather than
an error.

Still provisional: the live V3.16 root folder name, nine MSWX variable folders, MSWX's `NRT` window and its
`3hourly` folder.

## Sidecar data

The share also carries a `Gauge_metadata/` folder of station CSVs (locations, date ranges, reporting-time
offsets) used in the gauge-correction step. Fetching those is not yet wired into this backend.
