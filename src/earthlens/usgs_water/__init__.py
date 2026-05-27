"""USGS NWIS / Water Data backend.

Thin wrapper over the official `dataretrieval` SDK that pulls per-site
water observations from the U.S. Geological Survey's National Water
Information System — stream gauges, groundwater wells, and
water-quality sites across the United States — and returns them as a
long-format :class:`pandas.DataFrame`.

This is a `tabular` backend: the result is a table of per-site
observations, not a gridded array, so :data:`USGSWater.OUTPUT_KIND` is
`"tabular"` and the :class:`earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument for it (use `service="statistics"` for
a server-side temporal rollup instead).

Request shape: `variables` is a `list[str]` of NWIS **parameter codes**
or friendly names — `variables=["00060"]`, `variables=["discharge",
"gage_height"]` — resolved to 5-digit codes via the bundled catalog.
The NWIS / Water Data service plane is selected by `service=` (default
`"daily"`); the modern / legacy endpoint by `api=` (default `"auto"`).
Authentication is an optional Personal Access Token.

Public surface (re-exported from this package):

* :class:`USGSWater` — the backend; instantiate with a date range, a
  bbox (or `sites=`), and `variables=[code_or_name, ...]`, then call
  :meth:`USGSWater.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `usgs_water_data_catalog.yaml` parameter-code table.
* :class:`Parameter` — one parameter code's catalog row (`code`,
  `name`, `units`, `group`, `services`).
* :class:`UsgsWaterAuth` — `AbstractAuth` implementation resolving the
  optional `API_USGS_PAT`.
* :class:`UsgsWaterCredentials` — frozen pydantic value object the auth
  class binds to (the optional token).
* :data:`CATALOG_PATH` — path to the bundled parameter YAML.
"""

from __future__ import annotations

from earthlens.usgs_water.auth import UsgsWaterAuth, UsgsWaterCredentials
from earthlens.usgs_water.backend import USGSWater
from earthlens.usgs_water.catalog import CATALOG_PATH, Catalog, Parameter

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Parameter",
    "USGSWater",
    "UsgsWaterAuth",
    "UsgsWaterCredentials",
]
