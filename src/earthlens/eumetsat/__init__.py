"""EUMETSAT Data Store backend (181 collections via `eumdac`).

One unified backend over the EUMETSAT Data Store: a single OAuth2
consumer key / secret mints a bearer token that reaches every
collection — MTG-I1 FCI, MSG SEVIRI, Metop (ASCAT / IASI), Metop-SG, the
Sentinel-3 / -5P / -6 mirrors, and the OSI SAF / CDR / FDR families. The
backend fetches whole native products to disk, and supports server-side
subset / reproject / reformat via the `tailor=TailorConfig(...)` Data
Tailor path; native SEVIRI / FCI client-side reading (the satpy bridge)
is a deferred follow-on.

Like the NASA Earthdata backend, the output shape is **per-collection,
not fixed** — `EUMETSAT` sets `OUTPUT_KIND` from the resolved catalog
row (`raster` / `vector` / `tabular`).

Public surface (re-exported from this package):

* `EUMETSAT` — the backend itself; instantiate with a date range, a
  bbox, and a `{collection_key: [selector, ...]}` mapping, then call
  `EUMETSAT.download`.
* `Catalog` — pydantic-backed loader for the bundled per-group
  `catalog/` directory.
* `EumetsatDataset` — one curated dataset (collection) row (collection_id,
  group, output_kind, format, selectors, tailor_product_type, extent).
* `DataStoreGroup` — the Data Store group (mission family) enum.
* `Extent` / `TemporalCoverage` — the spatial / temporal coverage rows.
* `EumetsatAuth` — `AbstractAuth` wrapper over `eumdac.AccessToken`.
  Idempotent; safe to call repeatedly.
* `EumetsatCredentials` — frozen value object the auth class binds to.
* `TailorConfig` — frozen request shape for the Data Tailor server-side
  subset / reproject / reformat path (`download(tailor=...)`).
* `AuthenticationError` — raised when token minting fails; subclass of
  `earthlens.base.AuthenticationError`.
* `CATALOG_PATH` — absolute path to the bundled `catalog/` directory.

The `[eumetsat]` extra pulls `eumdac`. The `eumdac` import is lazy, so
this package imports without the extra installed.
"""

from __future__ import annotations

from earthlens.eumetsat.auth import (
    AuthenticationError,
    EumetsatAuth,
    EumetsatCredentials,
)
from earthlens.eumetsat.backend import EUMETSAT
from earthlens.eumetsat.catalog import (
    CATALOG_PATH,
    Catalog,
    DataStoreGroup,
    EumetsatDataset,
    Extent,
    TemporalCoverage,
    clear_catalog_cache,
)
from earthlens.eumetsat.tailor import TailorConfig

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "DataStoreGroup",
    "EUMETSAT",
    "EumetsatAuth",
    "EumetsatDataset",
    "EumetsatCredentials",
    "Extent",
    "TailorConfig",
    "TemporalCoverage",
    "clear_catalog_cache",
]
