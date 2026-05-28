"""WorldPop backend — open population data hub over anonymous HTTPS.

`earthlens.worldpop` wraps the WorldPop open population data hub
(`hub.worldpop.org`): global gridded population counts, density, age/sex
structures, births, pregnancies, dependency ratios, urban change,
built-settlement growth, and forward projections — per country (ISO3) and
as global mosaics, at 100 m / 1 km, in constrained and unconstrained
variants. A request is an AOI (ISO3 / bbox / `GeoDataFrame`) + time window +
a list of WorldPop product aliases; the backend queries the WorldPop REST
API for the matching GeoTIFF URLs, downloads them over anonymous HTTPS, and
uses `pyramids` to mosaic + crop to the AOI — writing population GeoTIFFs
and, for demographic products, a tidy age/sex table. `OUTPUT_KIND="mixed"`.

The provider is open + CC-BY-4.0, so `WorldPopAuth` is a no-op kept for
conformance with the package's `AbstractAuth` shape. The default REST path
needs only the core dependencies; the optional WorldPopPy path imports
`worldpoppy` lazily (consuming only its file cache, never its `xarray`
return), so the package imports without the `[worldpop]` extra.
"""

from __future__ import annotations

from earthlens.base.auth import AuthenticationError
from earthlens.worldpop.auth import (
    WORLDPOP_ATTRIBUTION,
    WORLDPOP_LICENCE_URL,
    WorldPopAuth,
    WorldPopCredentials,
)
from earthlens.worldpop.backend import WorldPop
from earthlens.worldpop.catalog import (
    CATALOG_PATH,
    GENERATIONS,
    Catalog,
    Product,
    SubAlias,
)

__all__ = [
    "WorldPop",
    "WORLDPOP_ATTRIBUTION",
    "WORLDPOP_LICENCE_URL",
    "WorldPopAuth",
    "WorldPopCredentials",
    "AuthenticationError",
    "Catalog",
    "Product",
    "SubAlias",
    "GENERATIONS",
    "CATALOG_PATH",
]
