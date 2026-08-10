# ISIMIP bias-adjusted climate forcing — introduction

`earthlens.isimip` exposes the **ISIMIP** repository of **bias-adjusted,
impact-model-ready climate forcing** — the CMIP6-derived, W5E5-bias-corrected
input that hydrology / flood / agriculture impact models actually consume. It is
registered on the `EarthLens` facade under the key `"isimip"`.

The ISIMIP repository is **public** (no credentials). Search and the mandatory
server-side cutout run through the `isimip-client` SDK, so this backend has one
small extra:

```bash
pip install "earthlens[isimip]"   # adds isimip-client
```

## Why this backend exists — `isimip` vs `cmip6`

Both `cmip6` and `isimip` give you CMIP6-lineage climate futures, but they are
**not** the same thing, and `isimip` is not a duplicate:

- **`cmip6`** exposes the **raw** CMIP6 archive (the Pangeo `gs://cmip6` mirror)
  on each model's **native grid, unadjusted**. To drive an impact model with it
  you must bias-adjust and regrid it yourself against an observational reference
  — a non-trivial, error-prone step.
- **`isimip`** exposes the **already-bias-adjusted** derivative: CMIP6 GCMs
  statistically bias-corrected and downscaled against the **W5E5** observational
  dataset and packaged on a common grid, exactly as the ISIMIP impact-modelling
  community uses them. It is the pragmatic non-stationary-futures forcing — you
  skip the bias-adjustment entirely.

So reach for `isimip` when you want impact-ready forcing out of the box, and for
`cmip6` when you want the raw archive to process yourself. `earthlens.isimip`
deliberately does **not** re-expose raw CMIP6 — that is `cmip6`'s job.

## The request is a facet set

A request pins the ISIMIP facets — the simulation round (`dataset`), the GCM
(`gcm`), the scenario (`scenario`), one or more variables (`variables`), and the
time step (`temporal_resolution`):

| Facet | Argument | Example |
|-------|----------|---------|
| Simulation round | `dataset` | `"ISIMIP3b"` (scenarios) / `"ISIMIP3a"` (obs-based) |
| Climate forcing (GCM) | `gcm` | `"gfdl-esm4"`, `"ukesm1-0-ll"` (any casing) |
| Scenario | `scenario` | `"ssp585"`, `"ssp126"`, `"historical"` |
| Variable | `variables` | `["pr"]`, `["tas", "tasmax"]` |
| Time step | `temporal_resolution` | `"daily"` (default) / `"monthly"` |

The backend fetches the ISIMIP `InputData` product — the bias-adjusted climate
*forcing*; impact-model `OutputData` is out of scope. The facets are validated
against the bundled catalog vocabulary, so a typo in a GCM / scenario / variable
raises a clear did-you-mean error before any network call.

## The cutout is mandatory

A single ISIMIP global-daily granule is **~1–2 GB**, and a whole dataset is
**~18 GB**. `earthlens.isimip` therefore **never** pulls a granule whole for a
regional request: it submits a **server-side cutout job** (`isimip-client`'s
`cutout_bbox`) — submit the bbox → poll → download the cut file — so only the
requested box crosses the wire (a 2° × 2° European box of one decade granule is
~4 MB rather than ~1.2 GB).

Because of that, a request **must** give a bounding box (`lat_lim` / `lon_lim`),
or explicitly opt into the raw whole-globe pull with `whole_globe=True` (which
warns and downloads the multi-GB granules directly). `download()` returns the
`list[Path]` of the written NetCDF granules; reading / regridding them is
[pyramids](https://github.com/serapeum-org/pyramids)' job — `earthlens` never
imports `xarray` or `netCDF4`.

## Licence

ISIMIP licences are **per dataset** and read live from the repository. Most
ISIMIP3b **InputData** (the W5E5-bias-adjusted forcing) is **`CC0 1.0`** (public
domain); some **OutputData** and sectoral inputs carry their own terms and are
flagged `restricted`. `earthlens.isimip` surfaces each dataset's actual rights
and emits a `LicenseWarning` only for a restricted / non-open dataset — cite
ISIMIP and the underlying GCM per the ISIMIP terms of use.
