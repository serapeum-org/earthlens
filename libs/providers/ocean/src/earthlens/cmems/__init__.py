"""Copernicus Marine Service backend (CMEMS).

Thin wrapper over :mod:`copernicusmarine` (v2.x) that subsets
oceanographic datasets — physics, biogeochemistry, sea-surface
temperature, sea level, sea ice, in-situ observations — server-side
and writes the result as a single NetCDF (or Zarr) per
`(dataset_id, variables)` tuple.

Public surface (re-exported from this package):

* :class:`CMEMS` — the backend itself; instantiate with a date range,
  a bbox, and a `{dataset_id: [variable, ...]}` mapping, then call
  :meth:`CMEMS.download` to subset every dataset/variable group.
* :class:`Catalog` — pydantic-backed loader for
  the bundled `catalog/` directory. Exposes the merged structure as
  typed pydantic fields (`available_datasets`, `datasets`).
* :class:`Dataset` — one CMEMS dataset's section inside the catalog
  (variables map plus cadence + temporal-coverage metadata).
* :class:`Variable` — one variable's metadata (units, long-name,
  flux/state marker).
* :class:`CmemsAuth` — `AbstractAuth` implementation that wraps
  `copernicusmarine.login`. Idempotent; safe to call repeatedly.
* :class:`CmemsCredentials` — frozen pydantic value object the auth
  class binds to (username + password + optional credentials file).
* :class:`AuthenticationError` — raised when `copernicusmarine.login`
  cannot authenticate; subclass of
  :class:`earthlens.base.AuthenticationError`.
* :data:`CATALOG_PATH` — absolute path to the bundled `catalog/`
  directory; monkey-patchable to redirect the loader at a temporary
  directory or single file.

The catalog ships with this package as data — a directory of
per-domain `*.yaml` files plus an `_index.yaml`, loaded and merged by
:class:`Catalog` from `Path(__file__).parent / "catalog"`.

Examples:
    - List curated datasets and look up a variable:

        ```python
        >>> from earthlens.cmems import Catalog
        >>> cat = Catalog()
        >>> "cmems_mod_glo_phy_my_0.083deg_P1D-m" in cat.datasets
        True
        >>> cat.get_variable(
        ...     "cmems_mod_glo_phy_my_0.083deg_P1D-m", "thetao"
        ... ).units
        'degrees_C'

        ```
"""

from __future__ import annotations

from earthlens.cmems.auth import (
    AuthenticationError,
    CmemsAuth,
    CmemsCredentials,
)
from earthlens.cmems.backend import CMEMS
from earthlens.cmems.catalog import CATALOG_PATH, Catalog, Dataset, Variable

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "CMEMS",
    "Catalog",
    "CmemsAuth",
    "CmemsCredentials",
    "Dataset",
    "Variable",
]
