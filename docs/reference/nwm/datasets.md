# NOAA National Water Model — products & configurations

The curated catalog (`nwm_data_catalog.yaml`) has two blocks: **products**
(what a file contains) and **configurations** (which operational run
produced it). A concrete S3 key is assembled from a
`(configuration, product, cycle, step)` tuple.

## Products

### `chrtout` — channel routing (`tabular`)

Streamflow and velocity on the ~2.7 M NHDPlus v2 stream reaches, indexed
by `feature_id` (COMID). S3 token `channel_rt`; ~14 MB per whole-CONUS
file. Retrospective store:
`s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr`.

| Variable | Units | Description |
|----------|-------|-------------|
| `streamflow` | `m3 s-1` | River channel flow rate |
| `velocity` | `m s-1` | River channel flow velocity |
| `nudge` | `m3 s-1` | Amount of total flow nudged by data assimilation |
| `qSfcLatRunoff` | `m3 s-1` | Runoff from terrain routing |
| `qBucket` | `m3 s-1` | Flux from the groundwater bucket |

### `ldasout` — land surface (`raster`)

Gridded Noah-MP land states on the 1 km CONUS grid. S3 token `land`;
~30 MB per whole-CONUS file. Retrospective store:
`s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/ldasout.zarr`.

| Variable | Units | Description |
|----------|-------|-------------|
| `SOIL_M` | `m3 m-3` | Volumetric soil moisture |
| `SNEQV` | `kg m-2` | Snow water equivalent |
| `SNOWH` | `m` | Snow depth |
| `ACCET` | `mm` | Accumulated total evapotranspiration |
| `FSNO` | `1` | Snow-cover fraction on the ground |

## Configurations (CONUS)

| Configuration | Cycles/day | Steps | Horizon | Ensemble |
|---------------|-----------|-------|---------|----------|
| `short_range` | 24 (hourly) | `f001`…`f018` | 18 h | — |
| `analysis_assim` | 24 (hourly) | `tm00`…`tm02` | look-back | — |
| `medium_range` | 4 (00/06/12/18 UTC) | `f001`…`f240` | 240 h | members 1–6 |

## S3 key layout

```
nwm.{YYYYMMDD}/{configuration-dir}/nwm.t{HH}z.{family}.{output}.{step}.{domain}.nc
```

* `{output}` is the product's S3 token (`channel_rt`, `land`).
* `{step}` is `f{NNN}` for forecasts or `tm{NN}` for analyses.
* For an **ensemble** the directory is `{family}_mem{N}` and the member
  rides on the output token (`channel_rt_1`).
* `{domain}` is `conus` for the curated configurations.

Example:
`nwm.20260526/short_range/nwm.t00z.short_range.channel_rt.f001.conus.nc`

## Beyond the curated subset

The catalog curates only the clean CONUS configurations (the MVP scope).
The live bucket carries **69** configurations in total — regional
(Alaska / Hawaii / Puerto Rico), coastal (`total_water`), forcing, and
the `_no_da` / `_extend` / `_blend` / `long_range` variants. List them
all with the refresh tool:

```bash
pixi run -e dev python tools/nwm/refresh_nwm_catalog.py
```

The retention window is a rolling archive (~500+ days as of 2026-05); the
audit tool reports it against the backend's auto-mode boundary:

```bash
pixi run -e dev python tools/nwm/audit_nwm_catalog.py
```
