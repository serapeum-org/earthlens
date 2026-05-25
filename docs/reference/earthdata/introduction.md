# NASA Earthdata — introduction

[NASA Earthdata](https://www.earthdata.nasa.gov/) is the public gateway
to the EOSDIS archive — petabytes of Earth-observation data spread
across twelve Distributed Active Archive Centers (DAACs). The
`earthlens.earthdata` backend reaches **nine user-relevant DAACs**
through a single [Earthdata Login (EDL)](https://urs.earthdata.nasa.gov)
account and the [`earthaccess`](https://earthaccess.readthedocs.io)
client, which wraps NASA's Common Metadata Repository (CMR) search and
the per-DAAC download / in-region S3 access:

| DAAC | CMR provider | Flagship products |
|------|-------------|-------------------|
| PO.DAAC | `POCLOUD` | MUR SST, SWOT, GRACE-FO, Jason-3 / Sentinel-6 |
| NSIDC | `NSIDC_CPRD` | ICESat-2, SMAP, BedMachine, sea-ice CDR |
| LP DAAC | `LPCLOUD` | MODIS, VIIRS, ASTER, ECOSTRESS, EMIT, GEDI, NASADEM |
| OB.DAAC | `OB_CLOUD` | PACE, ocean colour |
| GES DISC | `GES_DISC` | MERRA-2, GPM IMERG, GLDAS, OCO-2/3, AIRS, TEMPO |
| LAADS | `LAADS` | VIIRS Black Marble, MODIS L1 |
| ASF | `ASF` | Sentinel-1, ALOS, NISAR, OPERA |
| ORNL DAAC | `ORNL_CLOUD` | Daymet, GEDI L4B |
| ASDC | `LARC_CLOUD` | CERES, MISR, CALIPSO |

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); for credentials see
[Authentication](authentication.md); the curated dataset list and the
maintenance tooling are on the [Catalog](catalog.md) page; the rendered
API is the [Reference](earthdata.md) page.

## Why it matters here — the per-dataset output kind

Every other earthlens backend emits **one** fixed output shape: CHC,
ERA5, CMEMS, and ECMWF write gridded rasters; FDSN writes vector
events. Earthdata cannot: its holdings span

- gridded **raster** (GPM IMERG, MUR SST, MODIS, EMIT, OPERA),
- point / profile **vector** (GEDI L4A footprints, ICESat-2 ATL06/08
  photon products),
- and plain **tabular** (some ORNL field-campaign CSV).

So `EarthData` is the first backend whose `OUTPUT_KIND` is **resolved
per request** from the catalog row, not declared as a fixed class
attribute. The [`EarthLens`](../earthlens.md) facade reads that
per-instance value when it decides whether to forward an `aggregate=`
request (raster only — see [Usage](usage.md#aggregation)).

## What the MVP does (and defers)

The backend **fetches whole native granules**: it searches CMR for the
collections you name, scoped to your bounding box and time window, then
either downloads them over HTTPS (`earthaccess.download`, the safe
default anywhere) or — when you run inside AWS `us-west-2` and the
collection is cloud-hosted — streams them from S3 with rotating
credentials (`earthaccess.open`).

Server-side spatial / variable subsetting (Harmony) and ASF's richer
InSAR stack search are **deferred** to a later release; see
[Catalog → deferred features](catalog.md#deferred-features). Because of
that, the band names in your request are **informational** for a
whole-granule fetch — you receive the entire granule.

## Python version note

`earthaccess >= 0.18` requires **Python ≥ 3.12**, even though earthlens
core targets ≥ 3.11. Installing `earthlens[earthdata]` on 3.11 will not
resolve `earthaccess`; the other backends are unaffected. See
[Authentication](authentication.md#installation).
