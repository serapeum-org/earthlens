"""JRC PVGIS solar-radiation / PV time-series backend.

Direct, keyless REST client over the JRC PVGIS 5.3 non-interactive service
(`https://re.jrc.ec.europa.eu/api/v5_3/<tool>`). A request selects a tool via
`variables=["seriescalc"]` (hourly radiation / PV power) or `["tmy"]` (typical
meteorological year), samples the location(s) — a single point or a bbox
expanded to a point grid — issues one keyless `GET` per point (throttled to
the 30 req/s limit), parses the JSON into a long-format `pandas.DataFrame`
tagged with `lat`/`lon`, and returns it.

This is a `tabular` backend: the result is a per-coordinate hourly table, not
a gridded array, so `PVGIS.OUTPUT_KIND` is `"tabular"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument for it
(PVGIS already returns the resolved hourly / TMY series). There is no SDK, no
authentication, and no `pyramids` / `xarray` dependency — pure `requests` +
`pandas`.

Public surface (re-exported from this package):

* `PVGIS` — the backend; instantiate with a date range, a bbox (or a single
  point), and `variables=["seriescalc"]` / `["tmy"]`, then call
  `PVGIS.download`.
* `Catalog` — pydantic-backed loader for the bundled `pvgis_data_catalog.yaml`
  product table.
* `Product` — one PVGIS tool's catalog row (`tool`, `endpoint`,
  `default_params`, `columns`, `description`).
* `CATALOG_PATH` — path to the bundled product YAML.
"""

from __future__ import annotations

from earthlens.pvgis.catalog import (
    CATALOG_PATH,
    Catalog,
    Product,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "Product",
    "clear_catalog_cache",
]
