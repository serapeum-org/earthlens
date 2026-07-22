"""GHSL backend — JRC Global Human Settlement Layer over open HTTPS.

`earthlens.ghsl` wraps the JRC Global Human Settlement Layer: the open
global built-up + population grids (GHS-POP, GHS-BUILT-S/V/H/C, GHS-SMOD,
GHS-LAND, GHS-DUC) and the R2025A GHS-WUP projections. A request is a bbox
+ time window + a list of GHSL product keys; the backend resolves the
matching epochs, downloads the intersecting Mollweide tiles (or the
whole-globe file) over anonymous HTTPS, and uses `pyramids` to
reproject / mosaic / crop to the AOI — one GeoTIFF per `(product, epoch)`.
`OUTPUT_KIND="raster"`.

The provider is open + attribution-only, so `GhslAuth` is a no-op kept for
conformance with the package's `AbstractAuth` shape. The optional STAC
search path imports `pystac-client` lazily; the default deterministic-URL
path needs only the core dependencies, so the package imports without the
`[ghsl]` extra.
"""

from __future__ import annotations

from earthlens.base.auth import AuthenticationError
from earthlens.ghsl.auth import GHSL_ATTRIBUTION, GhslAuth, GhslCredentials
from earthlens.ghsl.backend import GHSL

from earthlens.ghsl.catalog import CATALOG_PATH, Availability, Catalog, Product

__all__ = [
    "GHSL",
    "GHSL_ATTRIBUTION",
    "GhslAuth",
    "GhslCredentials",
    "AuthenticationError",
    "Catalog",
    "Product",
    "Availability",
    "CATALOG_PATH",
]
