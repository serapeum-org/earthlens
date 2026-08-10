"""FABDEM bare-earth DEM backend for earthlens.

Exposes `FABDEM`, the `AbstractDataSource` backend that downloads FABDEM V1-2
(Forest And Buildings removed Copernicus DEM, ~30 m global bare-earth terrain)
from the University of Bristol open HTTPS file tree and localises it via
pyramids, plus its `Catalog` / `Dataset` catalog surface.
"""

from __future__ import annotations

from earthlens.fabdem.backend import FABDEM
from earthlens.fabdem.catalog import Catalog, Dataset, clear_catalog_cache

__all__ = ["FABDEM", "Catalog", "Dataset", "clear_catalog_cache"]
