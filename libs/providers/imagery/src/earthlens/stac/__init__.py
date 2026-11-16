"""Unified STAC-API + COG backend (Planetary Computer / CDSE / Earth Search).

One backend over the STAC API v1 + COG providers, which differ only in asset
signing. A request is `variables={collection_key: [asset, ...]}` plus a bbox +
date window; the backend returns gridded `raster` output (one Cloud-Optimized
GeoTIFF per `(collection, date)`).

Public surface (re-exported from this package):

* `STAC` — the backend; instantiate with a date range, a bbox, a
  `{collection_key: [asset, ...]}` request, and (optionally) an `endpoint`,
  then call `STAC.download`.
* `Catalog` — pydantic-backed loader for the bundled per-endpoint
  catalog under `src/earthlens/stac/catalog/`, exposing `endpoints`,
  `available_collections`, `datasets` (collections), and
  `get_collection` / `get_endpoint` / `resolve`.
* `Endpoint` / `Collection` / `Asset` / `Extent`
  — the frozen value objects the catalog is built from.
* `PlanetaryComputerSigner` / `EarthdataSigner` / `CDSESigner`
  / `CdseS3Signer` / `BdcTokenSigner` / `build_signer` — the provider signers,
  re-exported from `pyramids_eo.stac` (earthlens keeps no second copy), plus the
  `build_signer` factory, which wraps the selected signer so a STAC auth failure
  raises `earthlens.base.AuthenticationError` like every other backend. The
  generic `Signer` protocol and the `aws-requester-pays` signer come from
  `pyramids.stac`; the `anonymous` signer is pyramids-eo's `_AnonymousS3Signer`
  (which adds `AWS_NO_SIGN_REQUEST` and the opt-in-region endpoint pin).
* `alternate_preference_for` — which `alternate-assets` mirror a signer prefers
  over an asset's canonical href, keyed by signer name. `aws-requester-pays`
  prefers `s3`, because every USGS Landsat asset's canonical href is the
  login-gated HTTPS copy; every other signer reads the canonical href.
* `CATALOG_PATH` — absolute path to the bundled catalog directory;
  monkey-patchable to redirect the loader at a temporary directory.

The STAC SDK (`pystac-client` — the `[stac]` extra) is imported lazily, so the
`EarthLens` facade still imports without it.
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
from earthlens.stac.signers import (
    BdcTokenSigner,
    CdseS3Signer,
    CDSESigner,
    EarthdataSigner,
    PlanetaryComputerSigner,
    alternate_preference_for,
    build_signer,
)

__all__ = [
    "STAC",
    "Asset",
    "AuthenticationError",
    "BdcTokenSigner",
    "CATALOG_PATH",
    "CDSESigner",
    "Catalog",
    "CdseS3Signer",
    "Collection",
    "EarthdataSigner",
    "Endpoint",
    "Extent",
    "PlanetaryComputerSigner",
    "alternate_preference_for",
    "build_signer",
]
