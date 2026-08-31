"""Global topography / bathymetry DEM backend for earthlens.

`earthlens.bathymetry` fetches static global elevation grids — GEBCO
(15-arc-second topography + bathymetry) and NOAA ETOPO1 (1-arc-minute Ice
Surface and Bedrock) — subset on the server to a requested bbox and written
as GeoTIFF. Every shipped DEM is reached through one uniform NOAA ERDDAP
`griddap` transport (pinned live in the A1 gate): a bbox-subset NetCDF the
backend reads with `pyramids.netcdf.NetCDF` and writes to GeoTIFF — earthlens
never imports a competing array stack to touch the NetCDF.

The public surface is the :class:`~earthlens.bathymetry.backend.Bathymetry`
backend and the :class:`~earthlens.bathymetry.catalog.Catalog` of DEM rows.
"""

from __future__ import annotations

from earthlens.bathymetry._helpers import WcsServiceUnavailableError
from earthlens.bathymetry.backend import Bathymetry
from earthlens.bathymetry.catalog import Catalog, Dataset

__all__ = ["Bathymetry", "Catalog", "Dataset", "WcsServiceUnavailableError"]
