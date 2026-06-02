# NOAA National Water Model — introduction

`earthlens.nwm` wraps the **NOAA National Water Model (NWM) v3.0**, NOAA's
operational hydrologic model for the United States. NWM routes the
land-surface water budget onto the NHDPlus v2 river network and runs the
Noah-MP land-surface model on a 1 km grid, producing per-reach streamflow
and gridded land states several times an hour.

The backend reads the public, **anonymous** AWS bucket
`s3://noaa-nwm-pds` (unsigned S3 — no credentials), so it is registered
on the `EarthLens` facade under the keys `"nwm"` and
`"national-water-model"`.

```bash
pip install earthlens[nwm]      # pulls boto3 / botocore
```

## Two output kinds: stream-reach vs gridded

NWM is unusual among the earthlens backends because its output kind
depends on the product you ask for:

| Product key | NWM file | Contents | `OUTPUT_KIND` |
|-------------|----------|----------|---------------|
| `chrtout`   | `channel_rt` | Streamflow / velocity on ~2.7 M NHDPlus stream reaches, indexed by `feature_id` (COMID) | **`tabular`** |
| `lakeout`   | `reservoir`  | Reservoir inflow / outflow / water-surface elevation per `feature_id` | **`tabular`** |
| `coastal`   | `total_water`| Coastal sea-surface elevation on the SCHISM unstructured-mesh `node`s | **`tabular`** |
| `ldasout`   | `land`       | Gridded Noah-MP land states (soil moisture, snow, ET) on the 1 km grid | **`raster`** |
| `rtout`     | `terrain_rt` | Gridded ponded-water / water-table state on the 250 m routing grid | **`raster`** |
| `forcing`   | `forcing`    | Gridded meteorological forcing (precip, temperature, wind, radiation) | **`raster`** |

The feature/lake/node-indexed products are **not** lat/lon rasters — they
are `index × time` tables — so `OUTPUT_KIND` is set **per request** from
the product(s) you name. A single request must not mix the two kinds (it
raises `ValueError`); split it into one request per kind. See
[products & configurations](datasets.md) for the full variable lists.

Because neither product is a griddable raster that the temporal reducer
can read, `aggregate=` is rejected for the NWM backend.

## Two modes: operational vs retrospective

| Mode | Bucket | Format | Coverage |
|------|--------|--------|----------|
| `operational`   | `s3://noaa-nwm-pds` | NetCDF | a rolling window (~500+ days as of 2026-05) |
| `retrospective` | `s3://noaa-nwm-retrospective-3-0-pds` | **Zarr** | 1979–2023 reanalysis |

The mode auto-routes from the date window (recent → operational, old →
retrospective), or you can force it with `mode=`. The **operational
whole-file download** is the shipped capability.

## Whole-file download vs. subset / retrospective

An operational NWM file is **whole-CONUS** — one `channel_rt` file
(~14 MB) holds *every* one of the 2.7 M reaches at a single timestep, and
a `land` file is ~30 MB. A plain operational request (whole-Earth bbox,
no `sites=`) **downloads those whole files**.

A **subset** or the **retrospective** archive is *read* rather than
downloaded whole, through
[pyramids](https://github.com/serapeum-org/pyramids)'s
`pyramids.netcdf.LabeledDataset` (pyramids ≥ 0.29.0) — earthlens never
imports `xarray` / `zarr` itself; pyramids owns the read. For the
**tabular** products (`chrtout`, `lakeout`, `coastal`):

* a `sites=` selection (explicit `feature_id`s or USGS `gage_id`s),
* a bbox narrower than whole-Earth,
* `mode="retrospective"` (the 1.4 TB Zarr, always subset, never
  downloaded whole),

are opened anonymously + lazily, sliced, and written as a tidy
`feature_id × time` **Parquet** table.

For the **gridded** products (`ldasout`, `rtout`, `forcing`) an
**operational** bbox subset is read through `pyramids.netcdf.NetCDF.subset`
— each variable is windowed on its native grid and written as a
**GeoTIFF**. (`sites=` doesn't apply to a grid; a variable with an
interleaved vertical/layer dimension, e.g. `SOIL_M`, and the gridded
**retrospective** are deferred with a clear `NotImplementedError` — see
[usage](usage.md).)

## See also

* [Usage](usage.md) — the request shape and every keyword argument.
* [Available products & configurations](datasets.md) — the full catalog.
* [API reference](nwm.md) — the rendered module API.
