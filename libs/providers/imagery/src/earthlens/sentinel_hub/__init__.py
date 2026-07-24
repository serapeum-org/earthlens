"""Sentinel Hub server-side-render backend (defaults to Sentinel Hub on CDSE).

earthlens sends a bbox/geometry + time + an evalscript to one of Sentinel Hub's
request planes (Process / Async / Batch render → raster GeoTIFF; Statistical /
Batch-Statistical → tabular zonal stats); the server computes on-the-fly and
earthlens collects the result. A request is
`variables={collection_or_recipe: [band, ...]}` plus a bbox + date window, and
the plane is chosen by `api=` (auto-selected by size + whether `geometry=` was
given).

Public surface (re-exported from this package):

* :class:`SentinelHub` — the backend; instantiate with a date range, a bbox, a
  `{collection_or_recipe: [band, ...]}` request, and (optionally) `evalscript`,
  `resolution`, `endpoint`, `mosaicking_order`, `api`, `geometry`, then call
  :meth:`SentinelHub.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled two-layer catalog
  under `src/earthlens/sentinel_hub/catalog/`, exposing `datasets`
  (collections), `recipes`, `available_collections`, and
  `get_collection` / `get_recipe` / `is_recipe` / `resolve`.
* :class:`Collection` / :class:`EvalscriptRecipe` / :class:`Band` /
  :class:`Extent` / :class:`ResolvedRequest` — the frozen value objects the
  catalog is built from.
* :func:`read_evalscript` — read a bundled `.js` evalscript by name.
* :class:`SentinelHubAuth` / :class:`SentinelHubCredentials` — the OAuth2
  client-credentials auth wrapper and its credentials value object.
* :class:`AuthenticationError` — raised when no credentials are resolvable.
* :data:`CATALOG_PATH` / :data:`EVALSCRIPTS_PATH` — absolute paths to the bundled
  catalog directory and evalscript directory; monkey-patchable in tests.

The Sentinel Hub client (`[sentinel-hub]` extra) is imported lazily, so the
`EarthLens` facade still imports without it.
"""

from __future__ import annotations

from earthlens.sentinel_hub.auth import (
    AuthenticationError,
    SentinelHubAuth,
    SentinelHubCredentials,
)
from earthlens.sentinel_hub.backend import SentinelHub
from earthlens.sentinel_hub.catalog import (
    CATALOG_PATH,
    EVALSCRIPTS_PATH,
    Band,
    Catalog,
    Collection,
    EvalscriptRecipe,
    Extent,
    ResolvedRequest,
    read_evalscript,
)

__all__ = [
    "CATALOG_PATH",
    "EVALSCRIPTS_PATH",
    "AuthenticationError",
    "Band",
    "Catalog",
    "Collection",
    "EvalscriptRecipe",
    "Extent",
    "ResolvedRequest",
    "SentinelHub",
    "SentinelHubAuth",
    "SentinelHubCredentials",
    "read_evalscript",
]
