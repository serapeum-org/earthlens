"""OpenAQ v3 air-quality backend.

Thin wrapper over the OpenAQ v3 web service — the aggregator of >180
air-quality monitoring networks worldwide (US AirNow, EEA, UK AURN,
Sensor.Community, and many national networks) — that returns
ground-station pollutant measurements as a long-format
:class:`pandas.DataFrame`.

This is the package's first `tabular` backend: the result is a table
of per-row station observations, not a gridded array, so
:data:`OpenAQ.OUTPUT_KIND` is `"tabular"` and the
:class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=`
argument for it (use the server-side `temporal_resolution` rollup
instead).

Parameter selection: for this backend `variables` is a `list[str]` of
pollutant parameter names — `variables=["pm25"]`,
`variables=["pm25", "no2"]` — **not** data-variable names. This is an
intentional, documented overload (the facade makes `variables` a
required argument). Query filters (`max_locations`,
`temporal_resolution` rollup, the date window) arrive as explicit
:class:`OpenAQ` constructor keyword arguments.

Public surface (re-exported from this package):

* :class:`OpenAQ` — the backend; instantiate with a date range, a
  bbox, and `variables=[parameter, ...]`, then call
  :meth:`OpenAQ.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `openaq_data_catalog.yaml` parameter dispatch table.
* :class:`Parameter` — one pollutant's dispatch row (`id`, `name`,
  `units`, `display_name`, `group`).
* :class:`OpenaqAuth` — `AbstractAuth` implementation resolving the
  single `X-API-Key`.
* :class:`OpenaqCredentials` — frozen pydantic value object the auth
  class binds to (the optional API key).
* :class:`AuthenticationError` — raised when no API key resolves;
  subclass of :class:`earthlens.base.AuthenticationError`.
* :data:`CATALOG_PATH` — path to the bundled parameter YAML;
  monkey-patchable in tests.

Examples:
    - List the registered pollutant parameters:

        ```python
        >>> from earthlens.openaq import Catalog
        >>> sorted(Catalog().parameters)  # doctest: +NORMALIZE_WHITESPACE
        ['bc', 'co', 'no', 'no2', 'o3', 'pm10', 'pm25', 'pressure',
         'relativehumidity', 'so2', 'temperature']

        ```
"""

from __future__ import annotations

from earthlens.openaq.auth import (
    AuthenticationError,
    OpenaqAuth,
    OpenaqCredentials,
)
from earthlens.openaq.backend import OpenAQ
from earthlens.openaq.catalog import CATALOG_PATH, Catalog, Parameter

__all__ = [
    "AuthenticationError",
    "CATALOG_PATH",
    "Catalog",
    "OpenAQ",
    "OpenaqAuth",
    "OpenaqCredentials",
    "Parameter",
]
