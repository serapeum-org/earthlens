"""SoilGrids backend — ISRIC SoilGrids 2.0 soil properties over OGC WCS.

`earthlens.soilgrids` serves bbox subsets of the ISRIC SoilGrids 2.0 global
250 m soil-property maps (clay, sand, silt, cfvo, phh2o, cec, nitrogen, soc,
ocd, ocs, bdod) as GeoTIFFs (`OUTPUT_KIND="raster"`). A request is a bbox plus
a list of property ids and optional depths / quantiles; the backend expands it
into `(property, depth, quantile)` coverage triples and fetches each one
server-side over WCS, writing one GeoTIFF per triple.

The WCS transport lives in `pyramids` (`Dataset.from_wcs`, released in
pyramids 0.38.0) — SoilGrids' native grid is a custom Interrupted Goode
Homolosine (`EPSG:152160`) that PROJ cannot resolve, so the reader is handed
the IGH proj4 string (`IGH_PROJ4`) as its `coverage_crs` shim. There is no auth
module — SoilGrids is open, CC-BY 4.0. This subpackage imports no OGC-WCS SDK
and no array library directly; the WCS transport and GeoTIFF I/O are pyramids'
job.
"""

from __future__ import annotations

from earthlens.soilgrids._helpers import (
    DEFAULT_QUANTILE,
    IGH_PROJ4,
    SOILGRIDS_ATTRIBUTION,
    bbox_from_extent,
    coverage_id,
    expand_request,
)
from earthlens.soilgrids.backend import SoilGrids

from earthlens.soilgrids.catalog import CATALOG_PATH, Catalog, Property

__all__ = [
    "SoilGrids",
    "Catalog",
    "Property",
    "CATALOG_PATH",
    "coverage_id",
    "expand_request",
    "bbox_from_extent",
    "DEFAULT_QUANTILE",
    "IGH_PROJ4",
    "SOILGRIDS_ATTRIBUTION",
]
