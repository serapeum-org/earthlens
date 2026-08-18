"""GDACS multi-hazard disaster-alert backend.

Thin wrapper over the public GDACS SEARCH feed — the Global Disaster
Alert and Coordination System (JRC / UN OCHA) — that returns
multi-hazard alerts (earthquakes, tropical cyclones, floods, volcanoes,
wildfires, droughts), each with a green/orange/red impact score, as a
pyramids :class:`~pyramids.feature.collection.FeatureCollection` of
geolocated alert features (CRS `EPSG:4326`).

This is a `vector` backend: the result is a table of alerts, not a
gridded array, so :data:`GDACS.OUTPUT_KIND` is `"vector"` and the
:class:`earthlens.earthlens.EarthLens` facade rejects an `aggregate=`
argument for it.

GDACS needs **no credentials** — the feed is public, so (like CHC)
there is no auth class and no `[gdacs]` extra to install; the only
dependency is `requests`, a core dep.

Hazard-type selection: for this backend `variables` is a `list[str]` of
GDACS hazard-type codes — `variables=["EQ"]`, `variables=["EQ", "TC"]`
— **not** data-variable names. This is an intentional, documented
overload (the facade makes `variables` a required argument). The
alert-level filter (`Green`/`Orange`/`Red`) arrives as an explicit
`alert_level=` keyword argument.

Public surface (re-exported from this package):

* :class:`GDACS` — the backend; instantiate with a date range, a bbox,
  and `variables=[hazard_code, ...]`, then call :meth:`GDACS.download`.
* :class:`GdacsUnavailableError` — raised when the SEARCH feed is
  unavailable after the backend's retries (a transport error or a
  retry-worthy status that persisted); carries the originating
  `status_code`. A live e2e test catches it and skips.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `gdacs_data_catalog.yaml` hazard-type dispatch table.
* :class:`HazardType` — one hazard type's dispatch row (`name`,
  `description`).
* :func:`geojson_to_fc` / :func:`empty_fc` — the GDACS GeoJSON →
  FeatureCollection mapper and its empty-result counterpart.
* :data:`CATALOG_PATH` — path to the bundled hazard YAML;
  monkey-patchable in tests.

Examples:
    - List the registered hazard codes:

        ```python
        >>> from earthlens.gdacs import Catalog
        >>> Catalog().codes()
        ['DR', 'EQ', 'FL', 'TC', 'VO', 'WF']

        ```
"""

from __future__ import annotations

from earthlens.gdacs._helpers import GdacsUnavailableError
from earthlens.gdacs.backend import GDACS
from earthlens.gdacs.catalog import CATALOG_PATH, Catalog, HazardType
from earthlens.gdacs.events import empty_fc, geojson_to_fc

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "GDACS",
    "GdacsUnavailableError",
    "HazardType",
    "empty_fc",
    "geojson_to_fc",
]
