"""ISIMIP backend — bias-adjusted, impact-model-ready climate forcing.

Exposes the ISIMIP repository of **bias-adjusted, impact-model-ready climate
forcing** (CMIP6-derived) — the pragmatic non-stationary-futures input for
flood / hydrology impact models. Unlike the `cmip6` backend (raw CMIP6 on the
Pangeo mirror), ISIMIP data is already bias-corrected against W5E5 and formatted
for impact models, so users don't bias-adjust raw CMIP6 themselves.

A request is a *facet set* — `simulation_round`, `climate_forcing` (GCM),
`climate_scenario`, `climate_variable`, `time_step` (+ `product`) — which the
ISIMIP repository REST API resolves to the matching per-decade NetCDF granules.
Those granules are huge (~1-2 GB each; ~18 GB for a whole global-daily dataset),
so the backend is **file-writing** with a mandatory server-side cutout: it
submits an async cutout job (submit bbox -> poll -> download), returning the
`list[Path]` of NetCDF granules cut to the requested bbox. earthlens never
imports `xarray` / `netCDF4` — reading the NetCDF is pyramids'.

Public surface (re-exported from this package):

* `ISIMIP` — the backend; instantiate with a date window, a bbox, and a
  facet set (`dataset` / `gcm` / `scenario` / `variables`), then call
  `ISIMIP.download`.
* `Catalog` — loader for the bundled `isimip_data_catalog.yaml` (config +
  curated facet vocabulary).
* `Variable` / `Forcing` / `Scenario` / `Round` — one
  curated variable / forcing / scenario / round row.
* `CATALOG_PATH` — path to the bundled YAML; monkey-patchable in tests.
* `clear_catalog_cache` — empty the catalog parse cache.

Examples:
    - Resolve a curated variable's metadata:
        ```python
        >>> from earthlens.isimip import Catalog
        >>> Catalog().get_dataset("pr").long_name
        'Precipitation (all phases)'

        ```
"""

from __future__ import annotations

from earthlens.isimip.backend import ISIMIP
from earthlens.isimip.catalog import (
    CATALOG_PATH,
    Catalog,
    Forcing,
    Round,
    Scenario,
    Variable,
    clear_catalog_cache,
)

__all__ = [
    "CATALOG_PATH",
    "ISIMIP",
    "Catalog",
    "Forcing",
    "Round",
    "Scenario",
    "Variable",
    "clear_catalog_cache",
]
