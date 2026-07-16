"""Drought-indicator backend over three live sources (mixed output kind).

`earthlens.drought` is one backend that reaches three public drought-data
services with three different transports:

* **US Drought Monitor (USDM)** — weekly D0–D4 polygon classes (vector
  output) via the NDMC / UNL GeoJSON endpoint. `download()` returns a
  `pyramids.feature.collection.FeatureCollection` in EPSG:4326.
* **Copernicus EDO / GDO** — Standardised Precipitation Index, soil moisture
  anomaly, fAPAR anomaly, Combined Drought Indicator, GRACE TWS anomaly,
  and friends, served by the Copernicus EMS drought WCS (raster output).
  EDO/GDO is a REST shim, not a conformant WCS server — only its
  `GetCoverage` operation is reliable (the standard `GetCapabilities` /
  `DescribeCoverage` discovery handshake is 502 / 400). So the backend
  builds the documented `GetCoverage` URL by hand with core `requests`
  (`TIME=<date>` + `SELECTED_TIMESCALE=<NN>` + a `SUBSET=Long/Lat` bbox),
  streams the GeoTIFF, and opens it via `pyramids.dataset.Dataset.read_file`
  — no `owslib`, no GDAL WCS driver, no `xarray`.
* **CSIC SPEIbase** — global 0.5° monthly NetCDF of the Standardised
  Precipitation Evapotranspiration Index at scales 1–48 months (raster
  output), read through the already-shipped `pyramids.netcdf.NetCDF`.

The backend's `OUTPUT_KIND` is **per-instance** (`G1`): the resolved catalog
row's `output_kind` (`vector` for USDM, `raster` for the rest) is copied
onto the instance at construction time. The `EarthLens` facade then gates
`aggregate=` correctly — raster routes forward it to the
`earthlens.aggregate` time-window reducer; the vector USDM route rejects it
with `NotImplementedError` (no gridded reduction over polygon classes).

Authentication: none — all three sources are open (USDM public domain;
Copernicus EMS free reuse; SPEIbase CC-BY 4.0). The backend logs the
per-source attribution as a single info line on success (`G6`).

Public surface (re-exported from this package):

* `Drought` — the backend itself. Instantiate via the facade
  (`EarthLens("drought", dataset="usdm", lat_lim=[...], lon_lim=[...],
  start=..., end=...)`).
* `Catalog` — the sharded catalog loader (USDM, EDO, GDO, SPEIbase).
* `Dataset` — one curated row (transport, endpoint, coverage,
  output_kind, cadence, native_crs, timescale, license_note).
* `CATALOG_PATH` — absolute path to the bundled `catalog/` directory.
* `clear_catalog_cache` — drop the parse cache to force a re-read.

The backend pulls no new SDK extra: all three transports use core
`requests` plus the already-shipped `pyramids` reader stack (`Dataset` /
`NetCDF` / `FeatureCollection`). The `[drought]` extra is therefore
intentionally absent from `pyproject.toml`.
"""

from __future__ import annotations

from earthlens.drought.backend import Drought
from earthlens.drought.catalog import (
    CATALOG_PATH,
    Catalog,
    Dataset,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Dataset",
    "Drought",
    "clear_catalog_cache",
]
