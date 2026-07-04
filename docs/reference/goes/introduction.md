# NOAA GOES-R ABI — introduction

`earthlens.goes` wraps the **NOAA GOES-R series Advanced Baseline Imager
(ABI)** — the geostationary imager on GOES-East / GOES-West that scans the
Americas and the Pacific continuously. It fetches the raw ABI **NetCDF
granules** from the public, **anonymous** AWS Open-Data buckets, so it is
registered on the `EarthLens` facade under the key `"goes"`.

```bash
pip install earthlens[goes]     # re-spells [s3] — pulls boto3 / botocore
```

The backend **downloads whole granules and does not decode them** — reading,
georeferencing, and reprojecting the geostationary NetCDF to arrays is the job
of [pyramids](https://github.com/serapeum-org/pyramids) or
[`satpy`](https://satpy.readthedocs.io/) downstream. `earthlens.goes` never
imports `xarray` / `netCDF4` / `goes2go`; it ships the bytes and gets out of
the way (the same "fetch raw, decode downstream" decision as the JAXA P-Tree
backend).

## Satellites → buckets

GOES imagery lives on three anonymous buckets. Which satellite is *East* vs
*West* rotates as new satellites commission, so the role→bucket map lives in
the catalog, not in code:

| `satellite=` | Bucket | Role (as of 2025) |
|--------------|--------|-------------------|
| `"east"` / `"19"` | `noaa-goes19` | operational **GOES-East** (75.2°W) since 2025-04-07 |
| `"west"` / `"18"` | `noaa-goes18` | operational **GOES-West** (137°W) |
| `"16"` | `noaa-goes16` | on-orbit standby / historical archive |

## Products & domains

ABI publishes a **Level-1b radiance** family (`ABI-L1b-Rad`, one file per
spectral channel) and many **Level-2** science products (`ABI-L2-MCMIP`
multi-band cloud & moisture imagery, plus aerosol, cloud, land / sea surface
temperature, fire, precipitation, …). Each product is scanned over one or more
**domains**:

| `domain=` | Name | Cadence | S3 prefix suffix |
|-----------|------|---------|------------------|
| `"C"` | CONUS | 5 min | `…C` (e.g. `ABI-L2-MCMIPC`) |
| `"F"` | Full Disk | 10 min | `…F` |
| `"M1"` / `"M2"` | Mesoscale sub-sectors | 1 min | shared `…M` prefix, split by the filename token |

The two mesoscale sub-sectors share **one** `…M` S3 prefix and are told apart
only by the filename (`OR_ABI-L2-MCMIPM1-…` vs `…MCMIPM2-…`); the backend
handles that split for you. See [products & domains](datasets.md) for the
curated catalog.

## Whole-granule download — no server-side subset

S3 serves whole ABI NetCDF files; there is **no** bbox or band subsetting on the
server. A request downloads the whole granule and leaves any spatial crop /
band extraction to the downstream reader. Granules are sized accordingly:

| Product / domain | Approx. size |
|------------------|--------------|
| `abi-l2-mcmip` CONUS | ~58 MB |
| `abi-l2-mcmip` Full Disk | ~360 MB |
| `abi-l2-mcmip` Mesoscale | ~4 MB |
| `abi-l1b-rad` CONUS (per channel) | ~12–67 MB |

`variables=` selects **which granules** (which ABI channels) for the band-split
products (`abi-l1b-rad`, `abi-l2-cmip`); for the combined `abi-l2-mcmip` it is
informational, because one file already carries all 16 bands. Because the
granules are raw and undecoded, `aggregate=` is **rejected**.

## Licensing

GOES data is **US Government public domain** — no licence gate, attribution only.

## See also

* [Usage](usage.md) — the request shape and every keyword argument.
* [Products & domains](datasets.md) — the curated catalog.
* [API reference](goes.md) — the rendered module API.
