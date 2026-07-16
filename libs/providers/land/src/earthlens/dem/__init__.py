"""Anonymous Copernicus DEM backend.

Fetches raw Copernicus DEM GLO-30 / GLO-90 COG tiles from the public
AWS Open Data buckets — no account, no SDK login, no key. Cropping,
mosaicking, and reprojection are pyramids' job; this package returns
whole 1° tiles as they came from the bucket. See `docs/reference/dem/`
for the usage guide and the shipped example notebook for a
mosaic-with-pyramids walkthrough.
"""

from __future__ import annotations

from earthlens.dem.backend import DEM
from earthlens.dem.catalog import CATALOG_PATH, Catalog, DEMDataset, clear_catalog_cache

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "DEM",
    "DEMDataset",
    "clear_catalog_cache",
]
