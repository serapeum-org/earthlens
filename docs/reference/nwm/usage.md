# NOAA National Water Model — usage

## The request shape

NWM is two-axis: `variables = {product: [variable, ...]}` selects the
products, and `configuration=` picks the operational run that produced
them.

```python
from earthlens.core import EarthLens

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
| `configuration` | The operational run key — any of the 55 in `Catalog().available_configurations` (`"short_range"`, `"analysis_assim"`, `"medium_range"`, `"long_range"`, the `*_alaska` / `*_hawaii` / `*_puertorico` regional and `*_coastal_*` domains, the `forcing_*` runs, …). Default `"short_range"`. |
| `mode` | `"operational"` (NetCDF) or `"retrospective"` (Zarr — tabular products only). `None` (default) auto-routes by the date window. |
| `member` | Ensemble member (1-based) for an ensemble configuration (`medium_range`, members 1–6); ignored for deterministic runs. |
| `cycles` | Restrict the UTC run hours fetched (a subset of the configuration's run hours). Default: every cycle the configuration runs. |
| `steps` | Explicit forecast (`fNNN`) / analysis (`tmNN`) steps. Wins over `horizon`. |
| `horizon` | Maximum step; expands from the configuration's first step on its cadence. |
| `sites` | Explicit `feature_id`s and/or USGS `gage_id` strings to subset to (tabular products) — read + sliced through the pyramids reader into a Parquet table. |
| `lat_lim` / `lon_lim` | Bounding box. A **whole-Earth** box (`[-90, 90]` / `[-180, 180]`) means "no spatial subset" → whole-file download. A narrower box subsets the tabular products by their in-file lat/lon coords. |
| `path` | Output directory for the fetched NetCDF files. |

## What you get back

A plain (no-subset) request returns a `list[pathlib.Path]` — one
**whole-CONUS** NetCDF file per `(cycle, step, product)` it fetched. Each
output name flattens the full S3 key (`nwm.{date}_{config}_…`) so a
multi-day window stays unique (the NWM basename alone omits the date). A
`(cycle, step)` that is not yet published is logged and skipped (so one
gap does not lose the rest). Files are written atomically (a `.part`
rename).

A **subset** request returns the subset artefacts instead — `.parquet`
tables (tabular products) or `.tif` COGs (gridded products). The fetched
whole-CONUS `.nc` is **left in place** alongside them as the as-fetched
source, so a small-bbox request still produces a large `.nc` plus the
small output; delete the `.nc` yourself if you only want the subset.

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
temporal reduce needs a separate gridded reader. So
`download(aggregate=...)` raises `NotImplementedError`.

## Subsetting and the retrospective archive

Operational files are whole-CONUS, so a subset is *read* rather than
downloaded whole, through pyramids (≥ 0.38.0) — earthlens never imports
`xarray`/`zarr` itself.

**Tabular** products (`chrtout`, `lakeout`, `coastal`) — a `sites=` list,
a bbox, or the retrospective Zarr opens anonymously + lazily through
`pyramids.netcdf.LabeledDataset`, slices, and writes a tidy
`feature_id × time` **Parquet** table:

```python
# Retrospective streamflow for three reaches over a window -> Parquet
NWM(start="2010-06-01", end="2010-06-30",
    variables={"chrtout": ["streamflow"]},
    lat_lim=[-90, 90], lon_lim=[-180, 180],
    configuration="analysis_assim", mode="retrospective",
    sites=[101, 179, 181],          # feature_ids; USGS gage_id strings also work
    path="./nwm_out").download()    # -> [Path('chrtout_retro_20100601_20100630.parquet')]
```

**Gridded** products (`ldasout`, `rtout`, `forcing`) — an **operational**
bbox subset downloads the whole file, then `pyramids.netcdf.NetCDF.subset`
windows each variable on its native Lambert-Conformal-Conic grid and
writes one **GeoTIFF** per variable:

```python
# Operational gridded subset: snow water equivalent over a bbox -> GeoTIFF
NWM(start="2026-05-26", end="2026-05-26",
    variables={"ldasout": ["SNEQV"]},
    lat_lim=[39, 40], lon_lim=[-78, -75],
    configuration="analysis_assim", cycles=[0], steps=[0],
    path="./nwm_out").download()    # -> [Path('..._SNEQV.tif')]
```

Two gridded caveats:

* **`sites=` does not apply** to a gridded product (a grid has no
  `feature_id`) — use a bbox; passing `sites=` raises `ValueError`.
* A variable with a **vertical/layer dimension interleaved between its y
  and x axes** (e.g. `ldasout` `SOIL_M`, with 4 soil layers) cannot be
  windowed by the current reader and raises a clear `NotImplementedError`
  — request a single-level variable (`SNEQV`, `SNOWH`, `ACCET`, …) or
  download the whole file without a bbox.
* The gridded **retrospective** (`mode="retrospective"`) is **deferred**:
  the retro Zarr does not surface CF time units, so a `[start, end]`
  window cannot be mapped to the integer timesteps the reader selects by.
  It raises a clear `NotImplementedError`; use `mode="operational"` for a
  gridded bbox, or a tabular product for a retrospective time series.

## Catalog tooling

The federated `earthlens datasets` CLI keeps the catalog honest against the
live bucket (no standalone scripts — the same `refresh`/`audit` commands every
backend shares):

```bash
# List the live configurations and diff them against the bundled catalog.
earthlens datasets refresh nwm
# Flag any curated configuration no longer served live; exit non-zero on drift.
earthlens datasets audit nwm --strict
```

Both walk the most recent complete `nwm.YYYYMMDD/` day on the unsigned
`noaa-nwm-pds` bucket, collapsing ensemble-member directories to their base
configuration key. NWM's refreshable axis is its `available_configurations`
index; `--write` is not supported (the index is derived from the curated
rows, so `refresh nwm --write` reports "live read only").
