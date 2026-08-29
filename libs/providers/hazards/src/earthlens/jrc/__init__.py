"""JRC hazard backend for earthlens (EFHM + sea-level TWL forecasts).

Exposes `JRC`, the `AbstractDataSource` backend serving every JRC /
Copernicus-EMS hazard product from the open JRC HTTPS directory, dispatched on
the catalog row's `kind`: the European Flood Hazard Map (river-flood water depth
per return period, Europe and the Mediterranean Basin) and the probabilistic
sea-level Total Water Level forecasts (global gridded cubes + a per-country
coastal summary). Rasters are read with lazy `/vsicurl` windowed reads and
cropped to the AOI via pyramids. Also exposes the `Catalog` / `Dataset` surface.
"""

from __future__ import annotations

from earthlens.jrc.backend import JRC
from earthlens.jrc.catalog import Catalog, Dataset, clear_catalog_cache

__all__ = ["JRC", "Catalog", "Dataset", "clear_catalog_cache"]
