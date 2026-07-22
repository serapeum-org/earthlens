"""Generic ERDDAP backend — one client for many ERDDAP servers.

ERDDAP is a data-server protocol spoken by hundreds of independent
public servers (NOAA CoastWatch / Coral Reef Watch, NCEI, PacIOOS, …).
This backend reaches any of them from a single subpackage: a curated
**catalog of servers** pins each dataset to a concrete
`(server_url, dataset_id, protocol)`, and the `protocol` decides the
output shape — `griddap` datasets are gridded fields → **raster
NetCDF**, `tabledap` datasets are record tables → **tabular
`DataFrame`**. The backend therefore sets its `OUTPUT_KIND` **per
instance** from the resolved dataset (a sanctioned earthdata/eumetsat
override), so the :class:`earthlens.earthlens.EarthLens` facade accepts
`aggregate=` for a griddap dataset and rejects it for a tabledap one.

Built on the **`erddapy`** SDK (the tabledap `to_pandas()` path); the
griddap path builds the OPeNDAP `.nc` URL directly and downloads it,
then reads it back via **pyramids** — earthlens never imports `xarray`.
Only public (no-auth) servers ship in the catalog.

Public surface (re-exported from this package):

* :class:`ERDDAP` — the backend; instantiate with `dataset=<id>`, a
  date range, a bbox, and optional `variables=[...]`, then call
  :meth:`ERDDAP.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled sharded
  server catalog.
* :class:`Dataset` — one curated catalog row (`server_url`,
  `dataset_id`, `protocol`, `variables`, `dim_names`, …).
* :data:`CATALOG_PATH` — path to the bundled `catalog/` directory.
* :func:`clear_catalog_cache` — drop the module-level parse cache
  (tests that rewrite the catalog on disk).
"""

from __future__ import annotations

from earthlens.erddap.backend import ERDDAP

from earthlens.erddap.catalog import (
    CATALOG_PATH,
    Catalog,
    Dataset,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Dataset",
    "ERDDAP",
    "clear_catalog_cache",
]
