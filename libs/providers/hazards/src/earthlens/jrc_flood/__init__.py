"""JRC European flood-hazard (EFHM) backend for earthlens.

Exposes `JRCFlood`, the `AbstractDataSource` backend that reads the JRC European
Flood Hazard Map (river-flood water depth per return period, Europe and the
Mediterranean Basin) from the open JRC HTTPS directory via lazy `/vsicurl`
windowed reads and crops it to the AOI via pyramids, plus its `Catalog` /
`Dataset` catalog surface.
"""

from __future__ import annotations

from earthlens.jrc_flood.backend import JRCFlood
from earthlens.jrc_flood.catalog import Catalog, Dataset, clear_catalog_cache

__all__ = ["JRCFlood", "Catalog", "Dataset", "clear_catalog_cache"]
