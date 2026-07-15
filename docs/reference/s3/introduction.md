# Amazon S3 (AWS Open Data) — introduction

<img src="../../_images/logos/s3.png" alt="AWS Open Data logo" height="60">

`earthlens.s3` is the backend for **public AWS Open-Data datasets reached over
unsigned S3**. It is a *registry-driven, multi-dataset* backend: you pick a
`dataset` and answer one uniform request shape (`variables`, a lat/lon bbox, a
date window), and the catalog absorbs every per-provider difference (bucket, key
layout, on-disk format, CRS, variable/band naming) so **all datasets look the
same from the caller's side**.

## What it covers

The first-cut registry ships five curated datasets, all verified
unsigned-accessible (no credentials, public buckets):

| `dataset` | Bucket | Format | What it is |
|---|---|---|---|
| `era5` | `nsf-ncar-era5` | NetCDF | ECMWF ERA5 global reanalysis (hourly-in-monthly, 1940–present) |
| `sentinel-2-l2a` | `sentinel-cogs` | COG | Sentinel-2 L2A surface reflectance (per-band, MGRS tiles) |
| `goes` | `noaa-goes16` / `-goes18` | NetCDF | NOAA GOES ABI imagery (Americas, near-real-time) |
| `copernicus-dem` | `copernicus-dem-30m` / `-90m` | COG | Copernicus GLO-30/90 elevation (1° tiles, static) |
| `esa-worldcover` | `esa-worldcover` | COG | ESA WorldCover 10 m land cover (3° tiles, 2020/2021) |
| `usgs-landsat` ⚠️ | `usgs-landsat` (us-west-2) | COG | Landsat Collection-2 L2 surface reflectance / temperature — **requester-pays** |
| `naip-source` ⚠️ | `naip-source` (us-east-1) | COG | USDA NAIP 4-band aerial imagery (US only) — **requester-pays** |

⚠️ **Requester-pays** datasets need valid AWS credentials and **bill the caller's
AWS account** for every request/download — see [Authentication](authentication.md).
The other five are keyless/public.

Beyond the registry, an **inline passthrough** lets you point the backend at any
other public bucket using the same call shape (see [Usage](usage.md)).

`OUTPUT_KIND` is `"mixed"` (NetCDF + COG); every output is cropped to your AOI
and written to `path`.

## A note on the ERA5 bucket

The original Planet OS `era5-pds` bucket is **deprecated and no longer serves
unsigned requests** (every anonymous call returns `403`). AWS rehosted ERA5 at
NSF NCAR's `nsf-ncar-era5`, which is what `dataset="era5"` targets. The NCAR
layout is the full ERA5 archive organised by product stream
(`e5.oper.an.sfc`, …), 1940–present.

## Install

```bash
pip install earthlens[s3]
```

The `[s3]` extra pulls `boto3` / `botocore`. The package imports without the
extra; the friendly `ImportError` (naming `earthlens[s3]`) is raised only when
you actually build an S3 client.

## Authentication

None required — the buckets are public and the client is **unsigned**
(`botocore.UNSIGNED`). See [Authentication](authentication.md) for the rare
profile-signed case.
