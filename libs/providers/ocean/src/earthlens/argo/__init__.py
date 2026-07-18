"""Argo float-profile backend.

Thin wrapper over the official `argopy` SDK that pulls autonomous-float
ocean profiles — temperature, salinity, pressure, and (for BGC floats)
biogeochemical parameters — from the global Argo array, and returns them
as a long-format :class:`pandas.DataFrame`.

This is a `tabular` backend: Argo profiles are irregular point data (one
per float per ~10-day cycle), not a grid, so :data:`ARGO.OUTPUT_KIND` is
`"tabular"` and the :class:`earthlens.earthlens.EarthLens` facade rejects
an `aggregate=` argument for it (gridded ocean fields are the CMEMS
path).

Request shape: `variables` is either a list of Argo parameter names
(`["TEMP", "PSAL"]`) — a **region** selection over the request bbox + time
— or a single selector token: `"float:6902746"` (one or more floats by
WMO id) or `"profile:6902746/12"` (one float's cycle). The dataset
family is chosen with `dataset=` (`"phy"` default / `"bgc"`); the data
backend / QC mode / depth range with `source=` / `mode=` / `depth=`. Argo
is open data — there is no authentication and no `auth` module.

Public surface (re-exported from this package):

* :class:`ARGO` — the backend; instantiate with a date range, a bbox
  (or a `float:` / `profile:` selector in `variables`), then call
  :meth:`ARGO.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `argo_data_catalog.yaml` parameter-family vocabulary.
* :class:`Family` — one dataset family's parameter row.
* :data:`CATALOG_PATH` — path to the bundled catalog YAML.
"""

from __future__ import annotations

from earthlens.argo.backend import ARGO
from earthlens.argo.catalog import CATALOG_PATH, Catalog, Family, clear_catalog_cache

__all__ = [
    "ARGO",
    "CATALOG_PATH",
    "Catalog",
    "Family",
    "clear_catalog_cache",
]
