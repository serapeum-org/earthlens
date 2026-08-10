# DWD RADKLIM / RADOLAN — introduction

The `earthlens.radklim` backend fetches DWD's **gauge-adjusted radar
precipitation over Germany** from [DWD Open Data](https://opendata.dwd.de)
over anonymous HTTPS. It covers two streams:

- **RADKLIM** — the reprocessed, **climatologically consistent** radar
  climatology (1 km grid, 2001–present), in an hourly product (`RW`) and a
  5-min product (`YW`). This is the dataset to use for **statistics**.
- **RADOLAN** — the **operational**, near-real-time stream (same grid and
  products), served on a rolling ~2-day retention window. Use it for
  near-real-time monitoring, not for return periods.

It is a **raster** backend: `download()` returns the `list[Path]` of the raw
granules it fetched. Reading them (NetCDF / HDF5) is done downstream by
[pyramids](https://github.com/serapeum-org/pyramids) — earthlens never imports
`wradlib` / `xarray` / `netCDF4`.

## RADKLIM vs RADOLAN — which to use

| | RADKLIM (`radklim-rw` / `radklim-yw`) | RADOLAN (`radolan-rw` / `radolan-yw`) |
|---|---|---|
| Purpose | climatology / statistics | near-real-time monitoring |
| Homogeneity | reprocessed, consistent | operational, **inhomogeneous** |
| Coverage | full archive, 2001– | rolling **~2 days** only |
| Served as | one yearly `.tar.gz` NetCDF archive per year | per-timestamp `.hdf5` / `.bz2` granules |
| Return periods | reasonable (but ~25-yr record — see caveat) | **no** |

**RADKLIM-YW (5-min, 1 km) is the single most useful dataset for German
sub-hourly extreme rainfall and pluvial-flood work.** Caveat: the record is
only ~25 years, which is excellent for studying event *structure* but short for
fitting long return periods.

## What earthlens fetches (and what it does not)

- **reproc** products enumerate the **yearly** archives covering the requested
  window and download each — e.g. `YW2017.002_2024_netcdf.tar.gz` (~13.5 GB/yr
  for YW, ~836 MB/yr for RW). The reprocessing has no finer addressable unit on
  the NetCDF tree, so a `[start, end]` window maps to whole years.
- **operational** products read the stream's directory listing and download the
  per-timestamp granules whose scan time falls in the window (behind a
  retention guard — a window older than ~2 days returns nothing, with a warning
  pointing you at the RADKLIM archive).

## Formats and the decode boundary

- RADKLIM **NetCDF** (inside the yearly `.tar.gz`) and operational **HDF5** are
  read directly by pyramids — the recommended, default formats.
- The operational **`.bz2` RADOLAN binary** (`data_format="bin"`) is an opt-in
  format that needs a `wradlib`/pyramids decoder; earthlens only fetches it.

## Grid, licence, authentication

- **Grid** — the fixed DWD RADOLAN polar-stereographic grid over Germany (1 km
  cells). earthlens ships the native-grid granule; it does **not** reproject or
  subset. A request whose bbox cannot overlap Germany is rejected.
- **Licence** — CC-BY-4.0 / GeoNutzV (DWD open geodata). **Attribution is
  required** — credit *Deutscher Wetterdienst (DWD)*.
- **Authentication** — none. The source is anonymous HTTPS.
