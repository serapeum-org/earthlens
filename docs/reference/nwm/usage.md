# NOAA National Water Model — usage

## The request shape

NWM is two-axis: `variables = {product: [variable, ...]}` selects the
products, and `configuration=` picks the operational run that produced
them.

```python
from earthlens import EarthLens

lens = EarthLens(
    data_source="nwm",
    variables={"chrtout": ["streamflow"]},   # product -> variables
    configuration="short_range",             # which run
    start="2026-05-26",
    end="2026-05-26",
    path="./nwm_out",
)
paths = lens.download()                       # -> list[pathlib.Path]
```

You can also drive the backend directly:

```python
from earthlens.nwm import NWM

nwm = NWM(
    start="2026-05-26", end="2026-05-26",
    variables={"ldasout": ["SOIL_M", "SNEQV"]},
    lat_lim=[-90, 90], lon_lim=[-180, 180],   # whole-Earth = no subset
    configuration="analysis_assim",
    cycles=[0, 12], steps=[0],
    path="./nwm_out",
)
paths = nwm.download()
```

## Keyword arguments

| Argument | Meaning |
|----------|---------|
| `variables` | `{product: [variable, ...]}`. The MVP downloads whole files, so the variable list is **validated** (unknown names raise) but every variable in the file is fetched. An empty list selects all of the product's variables. |
| `configuration` | The operational run key: `"short_range"`, `"analysis_assim"`, or `"medium_range"`. Default `"short_range"`. |
| `mode` | `"operational"` (NetCDF) or `"retrospective"` (Zarr, `PY-G`-gated). `None` (default) auto-routes by the date window. |
| `member` | Ensemble member (1-based) for an ensemble configuration (`medium_range`, members 1–6); ignored for deterministic runs. |
| `cycles` | Restrict the UTC run hours fetched (a subset of the configuration's run hours). Default: every cycle the configuration runs. |
| `steps` | Explicit forecast (`fNNN`) / analysis (`tmNN`) steps. Wins over `horizon`. |
| `horizon` | Maximum step; expands from the configuration's first step on its cadence. |
| `sites` | Explicit `feature_id`s / USGS gage ids to subset to — **`PY-G`-gated** (raises). |
| `lat_lim` / `lon_lim` | Bounding box. A **whole-Earth** box (`[-90, 90]` / `[-180, 180]`) means "no spatial subset" — required to download whole files. A narrower box is a crop and is **`PY-G`-gated**. |
| `path` | Output directory for the fetched NetCDF files. |

## What you get back

`download()` returns a `list[pathlib.Path]` — one **whole-CONUS** NetCDF
file per `(cycle, step, product)` it fetched. A `(cycle, step)` that is
not yet published is logged and skipped (so one gap does not lose the
rest of the request). Files are written atomically (a `.part` rename).

Each `channel_rt` file is ~14 MB (all 2.7 M reaches at one timestep) and
each `land` file is ~30 MB, so a multi-cycle, multi-step request can be
large — narrow `cycles=` / `steps=` to keep it small.

## Cycles and steps

* **`short_range`** runs **hourly** (24 cycles/day) out to an **18 h**
  forecast horizon (`f001`…`f018`).
* **`analysis_assim`** runs **hourly** and publishes a short look-back
  (`tm00`…`tm02`) — the best-estimate nowcast.
* **`medium_range`** runs **4×/day** (00/06/12/18 UTC) out to **240 h**,
  as a 6-member ensemble (the member rides on the file name,
  `channel_rt_1`).

```python
# A 240-hour medium-range ensemble member-2 streamflow forecast, every
# 6 hours from the 00z cycle:
NWM(start="2026-05-26", end="2026-05-26",
    variables={"chrtout": ["streamflow"]},
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    configuration="medium_range", member=2,
    cycles=[0], steps=list(range(6, 241, 6)),
    path="./nwm_out").download()
```

## Why `aggregate=` is rejected

`chrtout` is feature-id-indexed (not a griddable raster), and a gridded
`ldasout` temporal reduce needs to *read* the file — a pyramids `PY-G`
capability. So `download(aggregate=...)` raises `NotImplementedError`.

## Subsetting and the retrospective archive

Operational files are whole-CONUS, so any subset (a `sites=` list, a
narrower bbox) needs a read, as does the retrospective Zarr. Until the
pyramids `PY-G` reader is released, these raise a clear
`NotImplementedError` naming `PY-G`:

```python
NWM(..., sites=[101]).download()                       # raises (PY-G)
NWM(..., lat_lim=[30, 40], lon_lim=[-100, -90]).download()  # raises (PY-G)
NWM(..., mode="retrospective").download()              # raises (PY-G)
```

## Catalog tooling

Two read-only scripts (not part of the installed package) keep the
catalog honest against the live bucket:

```bash
# Probe the bucket: retention window + the 69 live configurations.
pixi run -e dev python tools/nwm/refresh_nwm_catalog.py
pixi run -e dev python tools/nwm/refresh_nwm_catalog.py --format yaml

# Diff the curated catalog vs live; exit non-zero on drift.
pixi run -e dev python tools/nwm/audit_nwm_catalog.py --strict
```
