"""IUCN Red List assessment backend (`earthlens.iucn`).

Fetches Red List assessment records (category, criteria, population trend)
from the IUCN Red List v4 API through a thin direct `requests` shim — there
is no mature Python v4 client. Authenticated with a v4 token sent as an
`Authorization: Bearer` header (resolved by :class:`IucnAuth`); the cluster's
only tabular backend, returning a `pandas.DataFrame`. Every fetch raises a
`LicenseWarning` (CC-BY-NC; redistribution needs a written IUCN waiver). See
:class:`earthlens.iucn.backend.IUCN`.
"""

from __future__ import annotations

from earthlens.iucn.auth import (
    AuthenticationError,
    IucnAuth,
    IucnCredentials,
)
from earthlens.iucn.backend import IUCN
from earthlens.iucn.catalog import CATALOG_PATH, Catalog, Country

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "Country",
    "IUCN",
    "IucnAuth",
    "IucnCredentials",
]
