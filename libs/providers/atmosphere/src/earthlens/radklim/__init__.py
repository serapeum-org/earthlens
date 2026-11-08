"""DWD RADKLIM / RADOLAN gauge-adjusted radar-precipitation backend.

Downloads DWD's gauge-adjusted radar precipitation over Germany — the
reprocessed climatology **RADKLIM** (1 km, hourly `RW` / 5-min `YW`, 2001-,
climatologically consistent) and the operational near-real-time **RADOLAN**
stream — from DWD Open Data over anonymous HTTPS. A raster backend:
`download()` returns the `list[Path]` of raw granules (RADKLIM yearly NetCDF
archives, operational HDF5 / binary granules); reading them (NetCDF / HDF5 via
pyramids) is downstream.

Public surface (re-exported from this package):

* `RADKLIM` — the backend; instantiate with a time window, a bbox, and a
  `dataset=` product, then call `RADKLIM.download`.
* `Catalog` — loader for the bundled `radklim_data_catalog.yaml`.
* `RadklimProduct` — one product row.
* `CATALOG_PATH` — absolute path to the bundled catalog YAML.
* `GERMANY_ENVELOPE` — the fixed grid's Germany bounding box guard.

The provider is open + anonymous (CC-BY-4.0 / GeoNutzV, attribution required);
no credentials, no auth module. earthlens never imports `wradlib` / `xarray` /
`netCDF4` — decode is pyramids' job.
"""

from __future__ import annotations

from earthlens.radklim.backend import GERMANY_ENVELOPE, RADKLIM
from earthlens.radklim.catalog import CATALOG_PATH, Catalog, RadklimProduct

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "GERMANY_ENVELOPE",
    "RADKLIM",
    "RadklimProduct",
]
