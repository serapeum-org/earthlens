"""Protected Planet (WDPA) protected-area backend (`earthlens.wdpa`).

Fetches protected-area polygons from the World Database on Protected and
Conserved Areas through a thin direct Protected Planet v4 REST client
(`pywdpa` is not used — it targets the retired v3 API). Authenticated with a
personal token (`?token=` query param) via :class:`WdpaAuth`; every fetch
raises a `LicenseWarning` for the custom UNEP-WCMC terms. See
:class:`earthlens.wdpa.backend.WDPA`.
"""

from __future__ import annotations

from earthlens.wdpa.auth import (
    AuthenticationError,
    WdpaAuth,
    WdpaCredentials,
)
from earthlens.wdpa.backend import WDPA
from earthlens.wdpa.catalog import CATALOG_PATH, Catalog, Country

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "Country",
    "WDPA",
    "WdpaAuth",
    "WdpaCredentials",
]
