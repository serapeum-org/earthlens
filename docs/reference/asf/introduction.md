# ASF InSAR backend — introduction

The [Alaska Satellite Facility (ASF)](https://asf.alaska.edu/) holds
NASA's archive of synthetic aperture radar (SAR) — Sentinel-1, ALOS
PALSAR, NISAR, the OPERA processed-SAR family, and older missions —
and exposes search, download, and **InSAR baseline `stack()`**
through the official [`asf_search`](https://docs.asf.alaska.edu/asf_search/basics/)
Python SDK. earthlens ships an `asf` backend that wraps that SDK,
so a user can pull either a plain SAR catalog search or a
coregistered acquisition stack from a reference granule through the
same `download()` shape every other backend uses.

This page orients the backend. For the hands-on walkthrough see
[Usage](usage.md); the rendered API is the [Reference](asf.md)
page.

## Why a separate backend (ASF is already in `earthdata`)

ASF SAR granules live behind NASA Earthdata Login (EDL) and are
discoverable through CMR, so the existing `earthlens.earthdata`
backend can already pull individual granules from ASF. The reason
for a dedicated `asf` backend is **InSAR**: building a baseline
stack — finding the coregistered set of acquisitions inside
perpendicular- and temporal-baseline windows from a reference scene
— is something CMR cannot do. `asf_search.ASFProduct.stack()` is
the only path, and exposing it through earthlens makes the
InSAR-ready stack available without leaving the `EarthLens()` /
`download()` flow.

The two backends are deliberately complementary:

| Job | Use |
|---|---|
| Pull a single named ASF granule by id | `earthdata` (CMR is good at "find by id") |
| Find every Sentinel-1 SLC over a bbox + window | either; `asf` is slightly more direct |
| Build an InSAR baseline stack from a reference scene | **only `asf`** |
| Download the stack into a folder | `asf` (it reuses the EDL auth `earthdata` already minted) |

## Authentication

**Search runs anonymously**; only the **download** step requires an
EDL bearer token. The backend reuses
[`earthlens.earthdata.EarthdataAuth`](../earthdata/auth.md), so the
same credential ladder applies:

1. `EARTHDATA_TOKEN` (a JSON Web Token generated from the EDL profile).
2. `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD` env vars.
3. A `urs.earthdata.nasa.gov` entry in `~/.netrc`.

After a successful login, `EarthdataAuth` exposes the bearer token
on its `earthaccess.Auth` handle (`_auth.token["access_token"]`),
which the ASF wrapper hands to `asf_search.ASFSession().auth_with_token(...)`.
There is **no second credential system** to configure — if your
Earthdata account already works for the `earthdata` backend, it
works for `asf` too.

Register a free account at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov).

## Products and the `stackable` flag

The catalog (`asf_data_catalog.yaml`) curates one row per friendly
product key. Each row carries either an `asf.PLATFORM` member
(`SENTINEL1`, `ALOS`, `NISAR`, …) or an `asf.DATASET` member
(`OPERA_S1`, `ARIA_S1_GUNW`, `SLC_BURST` — the processed-product
families) plus an `asf.PRODUCT_TYPE` member (`SLC`, `BURST`,
`L1_1`, `RTC`, `CSLC`, …) and a `stackable: bool` flag.

| Product key | Description | `stackable` |
|---|---|---|
| `sentinel-1-slc` | Sentinel-1 Single Look Complex (the standard InSAR input) | **yes** |
| `sentinel-1-burst` | Sentinel-1 SLC burst (per-burst InSAR subset) | **yes** |
| `sentinel-1-grd` | Sentinel-1 Ground Range Detected, high-resolution dual-pol | no (search-only) |
| `alos-palsar-slc` | ALOS PALSAR Level-1.1 SLC | **yes** |
| `opera-cslc-s1` | OPERA S1 Coregistered Single Look Complex | **yes** |
| `opera-rtc-s1` | OPERA S1 Radiometric Terrain Corrected | no (processed, search-only) |
| `aria-s1-gunw` | ARIA S1 Geocoded Unwrapped Interferogram | no (precomputed) |
| `nisar-rslc` | NISAR Radar Single Look Complex | yes (post-launch) |

`stackable: false` means `ASFProduct.stack()` returns an empty
result for that product class — those products are *outputs* of
the InSAR pipeline (RTC, GUNW) or do not have a baseline-comparable
acquisition (GRD). Trying to use them in stack mode raises a
`ValueError` at construction.

Pass a friendly alias (`s1-slc`, `opera-rtc`, …) and the catalog
resolves it to the curated key with a did-you-mean hint on a typo.

## What a request returns

`download()` returns the list of written SAR product paths
(`list[Path]`), exactly as the file-writing backends (CHIRPS, S3,
ECMWF, GEE, earthdata, …) do. Idempotent re-runs skip files
already on disk — useful, because an SLC weighs in at hundreds
of megabytes.

The backend **does not crop / convert / unpack** the SAR products
in the MVP. An SLC is complex-valued (I/Q phase data), not a plain
raster you can bbox-crop; processed-derivative products (RTC, GRD)
are real-valued but the MVP's job is **retrieval + the stack**,
not InSAR processing. `download(aggregate=…)` therefore raises a
clear `NotImplementedError` — post-process the stack with a
dedicated InSAR tool ([HyP3](https://hyp3-docs.asf.alaska.edu/),
[SNAP](https://step.esa.int/main/toolboxes/snap/),
[MintPy](https://mintpy.readthedocs.io/),
[ISCE2/3](https://github.com/isce-framework/isce2)).

## Install

`asf_search` is an optional dependency. Install the extra:

```bash
pip install earthlens[asf]
```

This pulls `asf_search >=12.2.2` plus `earthlens[earthdata]` (for
the EDL auth ladder). The `earthlens` package imports without the
extra; only constructing the backend (or accessing the SDK) triggers
the lazy `import asf_search`.

## Aliases at the facade

```python
from earthlens.earthlens import EarthLens

EarthLens(data_source="asf",                    # canonical key
          ...)
EarthLens(data_source="alaska-satellite-facility",  # full-name alias
          ...)
EarthLens(data_source="insar",                  # capability alias
          ...)
```

All three resolve to `earthlens.asf.ASF`.
