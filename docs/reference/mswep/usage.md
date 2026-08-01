# MSWEP / MSWX — usage

Every example assumes access is configured — see [Authentication](authentication.md).

## The request shape

A granule is addressed by four coordinates, plus a fifth for MSWX:

| Coordinate | Argument | Values |
|---|---|---|
| Product | `product=` / the facade key | `mswep`, `mswx` |
| Version | `version=` | `2.80`, `3.15` (MSWEP); `1.00` (MSWX) |
| Variant | `variant=` | `Past`, `Past_nogauge`, `NRT` |
| Resolution | `temporal_resolution=` | `hourly`, `3hourly`, `daily`, `monthly` |
| Variable | `variables=` | `precipitation` (MSWEP); a **folder name** for MSWX |

That last row is the one to watch: MSWX shards by variable *above* the temporal folder, so the two products have
different Drive path shapes.

```text
MSWEP   MSWEP_V315/Past/Hourly/2020116.18.nc
MSWX    MSWX_V100/Past/Temp/Daily/2007133.nc
```

File names follow `YYYYDOY.HH` (hourly and 3-hourly, `HH` being the accumulation's starting hour), `YYYYDOY`
(daily) and `YYYYMM` (monthly).

## Daily precipitation

```python
from earthlens.core import EarthLens

paths = EarthLens(
    "mswep",
    start="2020-04-25",
    end="2020-04-30",
    variables=["precipitation"],
    temporal_resolution="daily",
    path="out",
).download()
```

## MSWX forcing variables

```python
paths = EarthLens(
    "mswx",                       # the alias already selects product="mswx"
    start="2007-05-13",
    end="2007-05-20",
    variables=["Temp"],           # a Drive folder name, not a netCDF field
    temporal_resolution="daily",
    path="out",
).download()
```

!!! note "Only `Temp` is confirmed today"
    Nine of MSWX's ten variable folder spellings could not be verified without a live share, so they are marked
    `provisional` in the catalog and **refused** rather than guessed at. If you have access, confirm the real
    names inside your share and drop the flags from `mswep_data_catalog.yaml`.

## Variants are chosen by date

`Past` and `Past_nogauge` cover **1979–2024**; `NRT` picks up at **2025** and runs to about two hours from real
time. Omit `variant=` and each timestep routes itself, so a window crossing the boundary spans both:

```python
EarthLens("mswep", start="2024-12-30", end="2025-01-02", ...).download()
# -> granules from Past *and* NRT
```

Name a variant explicitly and a window it cannot cover raises, pointing at the one that can.

## NRT: latency and revisions

NRT publishes at roughly **2 hours** latency, so the newest granules may not exist yet — those are logged and
skipped rather than raising. GloH2O then **rewrites NRT granules for about ten days** as better inputs land
("Users should redownload upgraded files"), so a local copy inside that window is stale, not cached: the backend
re-downloads it automatically. Older granules already on disk are reused.

## Where the files land

Granules mirror the share's own layout under `path=`, because a granule name is only unique **within its
folder** — MSWEP and MSWX share the `YYYYDOY.nc` stem, and all ten MSWX variables repeat it:

```text
out/MSWEP_V315/Past/Daily/2020116.nc
out/MSWX_V100/Past/Temp/Daily/2007133.nc
```

This is the same tree `rclone sync` produces, so a bulk pull and a targeted one can share a directory.

## Spatial subsetting is downstream

`lat_lim` / `lon_lim` are recorded for provenance but **do not shape the request** — GloH2O serves whole-globe
granules with no server-side crop. Clip after download with pyramids:

```python
from pyramids.netcdf import NetCDF

NetCDF.read_file(paths[0]).subset(bounds=(-20, 0, 55, 40))
```

## Errors worth catching

```python
from earthlens.mswep import DownloadQuotaExceededError, RateLimitedError
```

- `DownloadQuotaExceededError` — Drive's **per-file** cap, counted across everyone holding the share. Not
  cleared by retrying; wait ~24 h or use `rclone`.
- `RateLimitedError` — throttling or a transient 5xx, already retried with back-off before it surfaces.
- `ProvisionalValueError` — the request resolved onto a catalog value that has not been verified.
