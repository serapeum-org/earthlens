# CMIP6 climate projections — introduction

<img src="../../_images/logos/cmip6.svg" alt="WCRP CMIP6 logo" height="60">

`earthlens.cmip6` exposes the **raw, full CMIP6 archive** — the whole
`model × scenario × variable × member` matrix of the Coupled Model
Intercomparison Project Phase 6 — as analysis-ready cloud Zarr on the open
**Pangeo Google Cloud mirror** (`gs://cmip6`). It is registered on the
`EarthLens` facade under the keys `"cmip6"`, `"pangeo-cmip6"`, and
`"cmip6:climate-projections"`.

The archive is **anonymous** (no credentials, no auth), indexed by a single
plain consolidated-stores CSV. There is no per-backend SDK to install — the CSV
is read with `pandas` and the Zarr stores are read through **pyramids**' virtual
`/vsigs/` multidimensional reader, so the `cmip6` extra is empty:

```bash
pip install earthlens        # cmip6 needs nothing beyond the core install
```

## Why this backend exists (even though CMIP6 is already reachable)

Two shipped backends already touch CMIP6 — and neither gives you the archive:

- The `gee` backend exposes `NASA/GDDP-CMIP6` — a **single, pre-downscaled 1/4°
  BCSD product** (NEX-GDDP), not the archive.
- The `chc` backend exposes CHC-CMIP6 **precipitation deltas** only.

`earthlens.cmip6` is the only backend that exposes the **full** raw matrix: every
ScenarioMIP experiment (`ssp119` … `ssp585`) and CMIP DECK run (`historical`,
`piControl`, `1pctCO2`, `abrupt-4xCO2`, …), every Earth-system model, every
variant member, on each model's **native grid**. The canonical analysis-ready
copy of that whole archive lives on the open `gs://cmip6` bucket (Pangeo),
indexed by a CSV, with no auth — and that full-archive access is the entire
reason to build this.

Going *via* ESGF is the wrong default: ESGF flakiness is structural (the LLNL
node shut down in 2025), whereas the Pangeo ARCO CSV is a single stable path.

## The request is a facet tuple

A CMIP6 store is addressed by its **facets**. A request pins:

| Facet | Meaning | Example |
|-------|---------|---------|
| `source_id` | The model (GCM / ESM) | `CanESM5`, `GFDL-ESM4` |
| `experiment_id` | The scenario / diagnostic experiment | `ssp585`, `historical` |
| `variable_id` | The CMIP6 variable | `tas`, `pr`, `tos` |
| `table_id` | The MIP table (realm × cadence) | `Amon`, `day`, `Omon` |
| `member_id` *(optional)* | The variant label | `r1i1p1f1` (the default) |
| `grid_label` *(optional)* | Native (`gn`) vs regridded (`gr`) | `gn` |
| `version` *(optional)* | Publication version | `latest` (the default) |

The resolver filters the consolidated-stores CSV by this tuple to the matching
`zstore` (`gs://cmip6/...`) URI(s). A tuple that leaves `grid_label` (or
`member_id`) unpinned and matches more than one store **fans out** — one output
NetCDF per store. Unknown facet values raise a clear error listing the values
that *were* available, so a typo in a model or scenario name is easy to fix.

## Reads via pyramids — earthlens resolves, pyramids decodes

earthlens **resolves and requests**; **pyramids opens the Zarr, windows it, and
writes NetCDF**. `download()` maps the `[start, end]` date window to an integer
time-index range and the `lat_lim` / `lon_lim` box to a grid crop, then has
pyramids read only the requested cells and write one gridded NetCDF per store.
earthlens never imports `xarray` / `zarr` / `gcsfs` — the Zarr read is entirely
pyramids' (via `/vsigs/`, read anonymously; no `gcsfs` needed).

## Volumes — always subset

The stores are on each model's native grid and can be large (a full
monthly-atmosphere store spans the whole 1850–2100 record). **A bbox/time subset
is the default** and is what keeps a download tractable: the pyramids windowed
read fetches only the cells inside the box and the date window. A whole-grid
download (leaving `lat_lim` / `lon_lim` at whole-Earth) is allowed but warned,
and a whole-series read (`whole_time=True`) is opt-in.

## Attribution

CMIP6 output is provided for research use under each source model's CMIP6 terms
of use (most models are CC BY 4.0). **Cite the source GCM** and acknowledge the
CMIP6 project and the modelling group (WCRP CMIP6). The catalog carries a
per-model `terms_note`; `CMIP6.terms_note()` surfaces it for the requested model.
