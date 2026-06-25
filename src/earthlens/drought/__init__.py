"""Drought-indicator backend over three live sources (mixed output kind).

`earthlens.drought` is one backend that reaches three public drought-data
services with three different transports:

* **US Drought Monitor (USDM)** — weekly D0–D4 polygon classes (vector
  output) via the NDMC / UNL GeoJSON endpoint. `download()` returns a
  `pyramids.feature.collection.FeatureCollection` in EPSG:4326.
* **Copernicus EDO / GDO** — Standardised Precipitation Index, soil moisture
  anomaly, fAPAR anomaly, Combined Drought Indicator, GRACE TWS anomaly,
  and friends, served over **OGC WCS 2.0.0** (raster output). Each indicator
  is one catalog row routed through the temporal `pyramids.wcs.read_wcs`
  reader (the cross-repo `PY-A` task — pending pyramids release).
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

* `Catalog` — the sharded catalog loader (USDM, EDO, GDO, SPEIbase).
* `Dataset` — one curated row (transport, endpoint, coverage,
  output_kind, cadence, native_crs, license_note).
* `CATALOG_PATH` — absolute path to the bundled `catalog/` directory.
* `clear_catalog_cache` — drop the parse cache (tests).

The `Drought` backend class and the `EarthLens("drought", ...)` facade
entry land in the next task; this module ships the catalog + helpers
skeleton (`C1`).

The backend pulls no new SDK extra: USDM uses core `requests`, SPEIbase
reads through pyramids' shipped NetCDF stack, and the WCS reader lives in
pyramids (`PY-A`). The `[drought]` extra is therefore intentionally absent
from `pyproject.toml`.
"""

from __future__ import annotations

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
    "clear_catalog_cache",
]
