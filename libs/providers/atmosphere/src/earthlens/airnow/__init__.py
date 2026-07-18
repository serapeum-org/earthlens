"""AirNow ground-station air-quality backend.

Thin wrapper over the US EPA / Environment Canada AirNow `/aq/data/`
bounding-box service — reference-grade hourly monitor observations for
North America — that returns them as a long-format `pandas.DataFrame`
(one row per measurement), the same `tabular` shape as
`earthlens.openaq`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `AirNow.OUTPUT_KIND` is `"tabular"` and the
`earthlens.earthlens.core.EarthLens` facade rejects an `aggregate=` argument
for it.

Pollutant selection: for this backend `variables` is a `list[str]` of
pollutant names — `variables=["pm25"]`, `variables=["pm25", "o3"]` —
resolved to AirNow `parameters` codes via the bundled catalog. Query
filters (`data_type`, `monitor_type`, the date window) arrive as
explicit `AirNow` constructor keyword arguments.

Public surface (re-exported from this package):

* `AirNow` — the backend; instantiate with a date range, a bbox, and
  `variables=[pollutant, ...]`, then call `AirNow.download`.
* `Catalog` — pydantic-backed loader for the bundled
  `airnow_data_catalog.yaml` pollutant dispatch table.
* `Pollutant` — one pollutant's dispatch row (`name`, `code`, `units`,
  `display_name`, `group`).
* `AirnowAuth` — `AbstractAuth` implementation resolving the single
  `API_KEY`.
* `AirnowCredentials` — frozen pydantic value object the auth class
  binds to (the optional API key).
* `AuthenticationError` — raised when no API key resolves; subclass of
  `earthlens.base.AuthenticationError`.
* `CATALOG_PATH` — path to the bundled pollutant YAML; monkey-patchable
  in tests.

Examples:
    - List the registered pollutants:

        ```python
        >>> from earthlens.airnow import Catalog
        >>> sorted(Catalog().pollutants)
        ['co', 'no2', 'o3', 'pm10', 'pm25', 'so2']

        ```
"""

from __future__ import annotations

from earthlens.airnow.auth import (
    AirnowAuth,
    AirnowCredentials,
    AuthenticationError,
)
from earthlens.airnow.backend import AirNow
from earthlens.airnow.catalog import CATALOG_PATH, Catalog, Pollutant

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "AirNow",
    "AirnowAuth",
    "AirnowCredentials",
    "Catalog",
    "Pollutant",
]
