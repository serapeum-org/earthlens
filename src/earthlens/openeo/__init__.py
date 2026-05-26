"""openEO server-side-processing backend (defaults to CDSE openEO).

Builds an openEO process graph (`load_collection → recipe steps → aggregate →
save`), the backend executes it server-side, and earthlens downloads the gridded
`raster` result (GeoTIFF / NetCDF). A request is
`variables={collection_or_recipe: [band, ...]}` plus a bbox + date window.

Public surface (re-exported from this package):

* :class:`OpenEO` — the backend; instantiate with a date range, a bbox, a
  `{collection_or_recipe: [band, ...]}` request, and (optionally) `endpoint`,
  `process`, `execute`, `output_format`, `max_cloud_cover`, then call
  :meth:`OpenEO.download`.
* :class:`OpeneoAuth` / :class:`OpeneoCredentials` — the OIDC auth wrapper and
  its credentials value object.
* :class:`AuthenticationError` — raised when the OIDC flow fails.

The openEO client (`[openeo]` extra) is imported lazily, so the `EarthLens`
facade still imports without it.
"""

from __future__ import annotations

from earthlens.openeo.auth import (
    AuthenticationError,
    OpeneoAuth,
    OpeneoCredentials,
)
from earthlens.openeo.backend import OpenEO

__all__ = [
    "AuthenticationError",
    "OpenEO",
    "OpeneoAuth",
    "OpeneoCredentials",
]
