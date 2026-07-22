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
* :class:`Catalog` — pydantic-backed loader for the bundled two-layer catalog
  under `src/earthlens/openeo/catalog/`, exposing `datasets` (collections),
  `recipes`, `available_collections` / `available_processes`, and
  `get_collection` / `get_recipe` / `is_recipe` / `resolve`.
* :class:`Collection` / :class:`Recipe` / :class:`Extent` /
  :class:`ResolvedGraph` — the frozen value objects the catalog is built from.
* :class:`OpeneoAuth` / :class:`OpeneoCredentials` — the OIDC auth wrapper and
  its credentials value object.
* :class:`AuthenticationError` — raised when the OIDC flow fails.
* :data:`CATALOG_PATH` — absolute path to the bundled catalog directory;
  monkey-patchable to redirect the loader at a temporary directory.

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

from earthlens.openeo.catalog import (
    CATALOG_PATH,
    Band,
    Catalog,
    Collection,
    Extent,
    Recipe,
    ResolvedGraph,
)

__all__ = [
    "CATALOG_PATH",
    "AuthenticationError",
    "Band",
    "Catalog",
    "Collection",
    "Extent",
    "OpenEO",
    "OpeneoAuth",
    "OpeneoCredentials",
    "Recipe",
    "ResolvedGraph",
]
