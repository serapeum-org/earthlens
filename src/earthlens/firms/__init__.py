"""NASA FIRMS active-fire backend.

Thin wrapper over the NASA FIRMS (Fire Information for Resource
Management System) area CSV API that returns near-real-time and archival
**active-fire detections** from MODIS (C6.1) and VIIRS (S-NPP / NOAA-20 /
NOAA-21) as a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of fire-pixel
points (CRS `EPSG:4326`).

This is a `vector` backend: the result is a table of geolocated fire
detections, not a gridded array, so :data:`FIRMS.OUTPUT_KIND` is
`"vector"` and the :class:`earthlens.earthlens.EarthLens` facade rejects
an `aggregate=` argument for it.

FIRMS needs a free **`MAP_KEY`** (no SDK): the only dependencies are
`requests` + `pandas`, both core, so there is **no `[firms]` extra** to
install — the key lives in :class:`FirmsAuth`, not a dependency.

Sensor selection: for this backend `variables` is a `list[str]` of FIRMS
sensor codes — `variables=["VIIRS_SNPP_NRT"]`,
`variables=["MODIS_NRT", "VIIRS_SNPP_NRT"]` — **not** data-variable
names. This is an intentional, documented overload (the facade makes
`variables` a required argument). The detection filters
(`min_confidence=`, `day_night=`) arrive as explicit keyword arguments.

Public surface (re-exported from this package):

* :class:`FIRMS` — the backend; instantiate with a date range, a bbox,
  and `variables=[sensor_code, ...]`, then call :meth:`FIRMS.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `firms_data_catalog.yaml` sensor dispatch table.
* :class:`Sensor` / :class:`SensorColumn` — one sensor's row and one of
  its CSV columns.
* :class:`FirmsAuth` / :class:`FirmsCredentials` — `MAP_KEY` resolution.
* :class:`AuthenticationError` — raised when no usable `MAP_KEY` resolves.
* :func:`csv_to_fc` / :func:`empty_fc` — the FIRMS CSV → FeatureCollection
  mapper and its empty-result counterpart.
* :data:`CATALOG_PATH` — path to the bundled sensor YAML;
  monkey-patchable in tests.

Examples:
    - List the registered FIRMS sensor codes:

        ```python
        >>> from earthlens.firms import Catalog
        >>> Catalog().codes()
        ['MODIS_NRT', 'MODIS_SP', 'VIIRS_NOAA20_NRT', 'VIIRS_NOAA21_NRT', 'VIIRS_SNPP_NRT', 'VIIRS_SNPP_SP']

        ```
"""

from __future__ import annotations

from earthlens.firms.auth import (
    AuthenticationError,
    FirmsAuth,
    FirmsCredentials,
)
from earthlens.firms.backend import FIRMS
from earthlens.firms.catalog import (
    CATALOG_PATH,
    Catalog,
    Sensor,
    SensorColumn,
)
from earthlens.firms.events import csv_to_fc, empty_fc

__all__ = [
    "CATALOG_PATH",
    "AuthenticationError",
    "Catalog",
    "FIRMS",
    "FirmsAuth",
    "FirmsCredentials",
    "Sensor",
    "SensorColumn",
    "csv_to_fc",
    "empty_fc",
]
