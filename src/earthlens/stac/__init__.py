"""Unified STAC-API + COG backend (Planetary Computer / CDSE / Earth Search).

One backend over the STAC API v1 + COG providers, which differ only in asset
signing. A request is `variables={collection_key: [asset, ...]}` plus a bbox +
date window; the backend returns gridded `raster` output (one Cloud-Optimized
GeoTIFF per `(collection, date)`).

Public surface (re-exported from this package):

* :class:`STAC` — the backend; instantiate with a date range, a bbox, a
  `{collection_key: [asset, ...]}` request, and (optionally) an `endpoint`,
  then call :meth:`STAC.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled per-endpoint
  catalog under `src/earthlens/stac/catalog/`, exposing `endpoints`,
  `available_collections`, `datasets` (collections), and
  `get_collection` / `get_endpoint` / `resolve`.
* :class:`Endpoint` / :class:`Collection` / :class:`Asset` / :class:`Extent`
  — the frozen value objects the catalog is built from.
* :class:`MpcSasSigner` / :class:`CdseS3Signer` / :func:`build_signer` — the
  earthlens-side provider signers and the factory that selects one (the
  generic signers come from `pyramids.stac`).
* :data:`CATALOG_PATH` — absolute path to the bundled catalog directory;
  monkey-patchable to redirect the loader at a temporary directory.

The STAC SDKs (`pystac-client`, `planetary-computer` — the `[stac]` extra) are
imported lazily, so the `EarthLens` facade still imports without them.
"""

from __future__ import annotations

from earthlens.base import AuthenticationError
from earthlens.stac.backend import STAC
from earthlens.stac.catalog import (
    CATALOG_PATH,
    Asset,
    Catalog,
    Collection,
    Endpoint,
    Extent,
)
from earthlens.stac.signers import CdseS3Signer, MpcSasSigner, build_signer

__all__ = [
    "STAC",
    "Asset",
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "CdseS3Signer",
    "Collection",
    "Endpoint",
    "Extent",
    "MpcSasSigner",
    "build_signer",
]
