# ERDDAP — introduction

[ERDDAP](https://www.ncei.noaa.gov/erddap/information.html) is a data-server protocol spoken by hundreds of
independent public servers — NOAA CoastWatch / Coral Reef Watch, NCEI, PacIOOS, IOOS regional associations, and
many more. The `earthlens` ERDDAP backend reaches any of them from **one** subpackage: a curated **catalog of
servers** pins each dataset to a concrete `(server_url, dataset_id, protocol)`, and the `protocol` decides what
shape comes back.

For the hands-on download walkthrough see [Usage](usage.md); the curated server catalog is on the
[Available datasets](datasets.md) page; the rendered API is the [Reference](erddap.md) page.

## The two protocols — and why output is per-instance

A catalog row's `protocol` is the one thing you must understand up front, because it fixes both the request shape
and the return type:

* **`griddap`** — gridded fields (e.g. CRW daily 5 km SST anomaly, degree-heating-weeks, ocean-colour
  chlorophyll). Output is **raster NetCDF**, so the backend's `OUTPUT_KIND` is `"raster"` and the
  `EarthLens` facade **accepts** an `aggregate=` reduction.
* **`tabledap`** — record tables (e.g. NDBC buoy / glider time series). Output is a **tabular**
  `pandas.DataFrame`, so `OUTPUT_KIND` is `"tabular"` and the facade **rejects** `aggregate=` (a table has no
  gridded reduction).

Because the shape is decided by the chosen dataset, the backend sets `OUTPUT_KIND` **per instance** from the
resolved row (the same sanctioned override earthdata and eumetsat use) — there is no fixed class-level kind.

## What earthlens returns

* **`griddap`** → `download()` returns a `list[Path]` of written `.nc` files under the output directory. When you
  pass `aggregate=`, each NetCDF is reduced through the pyramids-backed `aggregate_netcdf` flow (exactly as the
  ECMWF backend does) and the returned paths are the per-window GeoTIFFs instead.
* **`tabledap`** → `download()` returns the `pandas.DataFrame`, and also writes it to disk as CSV (or Parquet via
  `output_format="parquet"`).

## How it is built

The backend is built on the **[`erddapy`](https://ioos.github.io/erddapy/)** SDK (IOOS, BSD-3), but only on the
tabledap path — the `ERDDAP.to_pandas()` call. The griddap path builds the OPeNDAP `.nc` download URL directly and
fetches it with a plain HTTP GET, deliberately **not** through the erddapy instance (whose `dataset_id` setter
eagerly downloads the dataset's entire coordinate axis — a slow metadata call). The downloaded NetCDF is read back
only through **pyramids**, so earthlens never imports `xarray`.

## Scope

Only **public** (no-auth) ERDDAP servers ship in the catalog, so there is no authentication step. ERDDAP servers
that require a login/token, live "search every dataset on a server" discovery, and a catalog-refresh tool are
deferred follow-ons.
