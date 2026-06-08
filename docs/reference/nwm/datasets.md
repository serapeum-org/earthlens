# NOAA National Water Model — products & configurations

The curated catalog (`nwm_data_catalog.yaml`) has two blocks: **products**
(what a file contains) and **configurations** (which operational run
produced it). A concrete S3 key is assembled from a
`(configuration, product, cycle, step)` tuple. The catalog covers **all
six products** and **all 55 operational configurations** on the bucket;
`Catalog().available_datasets` and `Catalog().available_configurations`
are the full indices.

## Products

| Key | NWM file | `OUTPUT_KIND` | Indexed by | Retrospective Zarr |
|-----|----------|---------------|------------|--------------------|
| `chrtout` | `channel_rt` | tabular | `feature_id` (NHDPlus reach) | `chrtout.zarr` |
| `lakeout` | `reservoir` | tabular | `feature_id` (reservoir) | `lakeout.zarr` |
| `coastal` | `total_water` | tabular | SCHISM mesh `node` | — |
| `ldasout` | `land` | raster | 1 km grid (`y`, `x`) | `ldasout.zarr` |
| `rtout` | `terrain_rt` | raster | 250 m routing grid | — |
| `forcing` | `forcing` | raster | model grid | `precip.zarr` |

Variable sets (units in parentheses), as read from the live files:

- **`chrtout`** — `streamflow` (m3 s-1), `velocity` (m s-1), `nudge` (m3 s-1), `qSfcLatRunoff` (m3 s-1), `qBucket` (m3 s-1), `qBtmVertRunoff` (m3).
- **`ldasout`** — `SOIL_M` (m3 m-3), `SNEQV` (kg m-2), `SNOWH` (m), `FSNO` (1), `ACCET` (mm), `SOILSAT_TOP` (1), `SNOWT_AVG` (K).
- **`lakeout`** — `inflow` (m3 s-1), `outflow` (m3 s-1), `water_sfc_elev` (m), `reservoir_type` (1), `reservoir_assimilated_value` (m3 s-1).
- **`rtout`** — `zwattablrt` (m), `sfcheadsubrt` (mm).
- **`forcing`** — `RAINRATE` (mm s-1), `T2D` (K), `Q2D` (kg kg-1), `U2D` (m s-1), `V2D` (m s-1), `PSFC` (Pa), `SWDOWN` (W m-2), `LWDOWN` (W m-2).
- **`coastal`** — `elevation` (m).

## Configurations

All 55 are curated. The major CONUS ones:

| Configuration | Cycles/day | Steps | Horizon | Ensemble |
|---------------|-----------|-------|---------|----------|
| `short_range` | 24 (hourly) | `f001`…`f018` | 18 h | — |
| `analysis_assim` | 24 (hourly) | `tm00`…`tm02` | look-back | — |
| `analysis_assim_extend` | 1 (16 UTC) | `tm00`…`tm27` | 28 h look-back | — |
| `analysis_assim_long` | 4 | `tm00`…`tm11` | 12 h look-back | — |
| `medium_range` | 4 (00/06/12/18) | `f003`…`f240` | 240 h | members 1–6 |
| `medium_range_blend` / `medium_range_no_da` | 4 | `f003`…`f240` | 240 h | — |
| `long_range` | 4 | `f006`…`f720` (6 h) | 720 h | members 1–4 |
| `forcing_*` | per family | — | — | — |

…plus the **regional** domains — `*_alaska`, `*_hawaii`, `*_puertorico`
(the Hawaii/Puerto Rico short-range runs are **sub-hourly**: 5-digit
`f00015`, `f00030`, … at a 15-minute cadence) — and the **coastal**
domains `*_coastal_{atlgulf,hawaii,pacific,puertorico}` (the `coastal`
product). Each carries the right `cycles_utc`, `step_kind`, `step_width`,
horizon, cadence, members, and product list. List them all:

```python
from earthlens.nwm import Catalog
cat = Catalog()
cat.available_configurations        # all 55 configuration keys
cat.get_config("short_range_hawaii").step_width   # 5 (sub-hourly)
```

## S3 key layout

```
nwm.{YYYYMMDD}/{configuration-dir}/nwm.t{HH}z.{family}.{output}.{step}.{domain}.nc
```

* The directory is the configuration key (`short_range`,
  `analysis_assim_hawaii`, `short_range_coastal_atlgulf`), with a
  `_mem{N}` suffix for an ensemble.
* `{family}` is the configuration's file-name token, `{output}` the
  product's S3 token (`channel_rt`, `land`, …); for an ensemble the
  member rides on the output token (`channel_rt_1`).
* `{step}` is `f{NNN}` for forecasts or `tm{NN}` for analyses,
  zero-padded to the configuration's step width (`f001`, `tm00`,
  `f00015`).

Example:
`nwm.20260526/short_range/nwm.t00z.short_range.channel_rt.f001.conus.nc`

## Tooling

```bash
# List the live configurations and diff them against the bundled catalog.
earthlens datasets refresh nwm
# Flag any curated configuration no longer served live; exit non-zero on drift.
earthlens datasets audit nwm --strict
```

Both commands walk the most recent complete `nwm.YYYYMMDD/` day on the
unsigned `noaa-nwm-pds` bucket (ensemble-member directories are collapsed to
their base configuration key). The assimilation-input `usgs_timeslices`
directory is a live configuration the catalog deliberately does not curate,
so it surfaces as an untracked id (informational), not drift.
