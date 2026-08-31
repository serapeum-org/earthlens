# ECMWF / Copernicus CDS — introduction

<img src="../../_images/logos/ecmwf.png" alt="Copernicus Climate Data Store (ECMWF) logo" height="60">

The `earthlens.ecmwf` backend downloads **ECMWF reanalyses from the
[Copernicus Climate Data Store](https://cds.climate.copernicus.eu)** (CDS) —
ERA5, ERA5-Land, CARRA, ORAS5, CMIP6 deltas, and their monthly-means siblings —
and slices the returned NetCDF into per-variable arrays cropped to your area and
date range. It is a thin, catalog-driven wrapper over the official
[`cdsapi`](https://pypi.org/project/cdsapi/) client.

| | |
|---|---|
| **Provider** | ECMWF Copernicus Climate Data Store |
| **Protocol** | `cdsapi` (async CDS queue + retrieve) |
| **`data_source` key** | `ecmwf` |
| **Extra** | `pip install earthlens[ecmwf]` (pulls `cdsapi`) |
| **Auth** | `~/.cdsapirc` token (see below) |
| **Output** | one NetCDF per `(dataset, variable)` |
| **Temporal resolution** | `"daily"` or `"monthly"` |

## What you request

Unlike the single-bbox backends, ECMWF takes a **`variables` mapping of
`dataset → [variable code, …]`**. The dataset short name (the mapping key) is a
CDS collection such as `reanalysis-era5-single-levels`; each variable code (e.g.
`2m-temperature`) is a slug the bundled catalog resolves to the exact CDS
request fields. The catalog ships ~37 curated datasets out of the ~134 CDS
publishes — see [Catalog & probe tooling](catalog.md) for the full structure and
how to extend it.

The backend expands `(dataset) × (variables)` into one CDS retrieve per
variable, validates each request against the dataset's `constraints.json`
before it consumes a queue slot, and writes the result as
`<path>/<cds_variable>_<dataset>.nc`.

Beyond CDS, the same backend reaches four more stores with the very same
token: the **Atmosphere Data Store (ADS)**, the **CEMS Early Warning Data Store
(EWDS)** — GloFAS flood forecasts, see [EWDS (GloFAS / floods)](ewds.md) — and
the two ECMWF-hosted stores, the **ECMWF Data Store (ECDS)** (TIGGE and S2S
ensemble forecasts) and the **Cross Data Store (XDS)** (fire fuel and burned
area); see [ECDS + XDS](ecds.md).

## Authentication

CDS access needs a personal API token in a `~/.cdsapirc` file:

```ini
url: https://cds.climate.copernicus.eu/api
key: <your-uid>:<your-api-key>
```

Create a free CDS account, accept each dataset's licence on its CDS page, then
copy your key from your CDS profile. `cdsapi` owns the authentication entirely;
the backend raises `AuthenticationError` if no usable credentials are found. See
[Authentication examples](../../examples/authentication.md) for a walkthrough.

## Pre-flight constraint checking

Every CDS dataset publishes a `constraints.json` describing the valid
combinations of its request fields. The backend runs a local `RequestValidator`
against those constraints **before** submitting, so an invalid request fails
fast on your machine instead of after sitting in the CDS queue. Pass
`skip_constraints=True` to bypass the pre-flight check when you know the request
is valid (or when CDS constraints are temporarily unavailable).

See [Usage](usage.md) for the request shape and a runnable example, and
[Aggregation](../../aggregation.md) for reducing the downloaded NetCDF stack
into windowed composites.
