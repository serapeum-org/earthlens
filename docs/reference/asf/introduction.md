# ASF InSAR backend — introduction

<img src="../../_images/logos/asf.png" alt="Alaska Satellite Facility (ASF) logo" height="60">

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
[`earthlens.earthdata.EarthdataAuth`](../earthdata/authentication.md)
— there is no second credential system to configure. Full setup,
the credential ladder, and the error path are documented under
[Authentication](authentication.md).

## Products and the `stackable` flag

The catalog ships **42 curated rows** covering Sentinel-1
(SLC / BURST / GRD / OCN / RAW + per-satellite variants), ALOS
PALSAR + ALOS-2, the OPERA-S1 family (RTC / CSLC / DIST-ALERT +
the OPERA-S1-CALVAL calibration companion), ARIA GUNW, the full
NISAR product family (RSLC / GSLC / GCOV / L0B / RIFG / RUNW /
GUNW / ROFF / GOFF / LRCLK_UTC), the TROPO atmospheric
corrections used by downstream InSAR processors, ERS-1/2,
JERS-1, RADARSAT-1, plus the SEASAT / SIR-C / AIRSAR / UAVSAR /
SMAP archive completeness rows. Each
row carries either an `asf.PLATFORM` member or an `asf.DATASET`
member, plus an `asf.PRODUCT_TYPE` member and a `stackable: bool`
flag.

`stackable: false` means `ASFProduct.stack()` returns an empty
result for that product class — those products are *outputs* of
the InSAR pipeline (RTC, GUNW) or do not have a baseline-comparable
acquisition (GRD). Trying to use them in stack mode raises a
`ValueError` at construction.

The full product table — every curated row with its SDK selector,
description, and aliases — lives under
[Available products](datasets.md).

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
from earthlens.core import EarthLens

EarthLens(data_source="asf",                    # canonical key
          ...)
EarthLens(data_source="alaska-satellite-facility",  # full-name alias
          ...)
EarthLens(data_source="asf:insar",                  # capability topic key
          ...)
```

All three resolve to `earthlens.asf.ASF`.
