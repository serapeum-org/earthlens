"""WRI Aqueduct riverine flood-risk backend (`aqueduct`).

Fetches the **WRI Aqueduct Global Flood Analyzer (2015)** riverine flood-risk
shapefiles from WRI's public file host (`files.wri.org`, CC-BY-4.0, no
credentials): the expected exposure of **GDP**, **population**, and **urban
area** to river flooding, aggregated by admin unit — **country**, **state**, or
**river basin** — across nine flood return periods (2 → 1000 yr), a 2010
baseline, and seven 2030 climate × socio-economic scenarios (RCP4.5 / RCP8.5 ×
SSP2 / SSP3). The admin polygons carry the risk attributes, so this is a
`vector` backend: `download()` returns a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` (or a geometry-dropped
`pandas.DataFrame`), and the :class:`earthlens.earthlens.EarthLens` facade
rejects an `aggregate=` argument.

This is the flood **impact / exposure** layer. It is distinct from the Aqueduct
flood **hazard** depth rasters, which are already reachable through the `gee`
backend as `WRI/Aqueduct_Flood_Hazard_Maps/V2`. The richer 2020 Aqueduct Floods
product (coastal flooding, 2050 / 2080, city level, pre-integrated
expected-annual damage) is not freely downloadable and is out of scope.

Public surface (re-exported from this package):

* :class:`Aqueduct` — the backend; instantiate with an `admin_level` / `metric`
  / `year` / `scenario` / `return_period` selection and call
  :meth:`Aqueduct.download`.
* :class:`Catalog` — loader for the bundled `aqueduct_data_catalog.yaml` (admin
  levels + the indicator / year / scenario / return-period vocabularies).
* :class:`AdminLevel` — one admin level's download + shapefile spec.
* :class:`Scenario` — one scenario's code and valid years.
* :data:`CATALOG_PATH` — path to the bundled catalog YAML; monkey-patchable in
  tests.
* :func:`clear_catalog_cache` — drop the module-level catalog parse cache.
"""

from __future__ import annotations

from earthlens.aqueduct.backend import Aqueduct
from earthlens.aqueduct.catalog import (
    CATALOG_PATH,
    AdminLevel,
    Catalog,
    Scenario,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "AdminLevel",
    "Aqueduct",
    "Catalog",
    "Scenario",
    "clear_catalog_cache",
]
