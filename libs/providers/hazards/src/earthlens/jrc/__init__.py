"""JRC European flood-hazard (EFHM) backend for earthlens.

Exposes `JRC`, the `AbstractDataSource` backend that reads the JRC European
Flood Hazard Map (river-flood water depth per return period, Europe and the
Mediterranean Basin) from the open JRC HTTPS directory via lazy `/vsicurl`
windowed reads and crops it to the AOI via pyramids, plus its `Catalog` /
`Dataset` catalog surface.
"""

from __future__ import annotations

from earthlens.jrc.backend import JRC
from earthlens.jrc.catalog import Catalog, Dataset, clear_catalog_cache

__all__ = ["JRC", "Catalog", "Dataset", "clear_catalog_cache"]
