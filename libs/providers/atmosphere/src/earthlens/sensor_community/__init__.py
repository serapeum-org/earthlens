"""Sensor.Community crowdsourced air-quality backend.

Returns readings from the Sensor.Community low-cost-sensor network as a
long-format `pandas.DataFrame` (one row per measurement), the same
`tabular` shape as `earthlens.openaq`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `SensorCommunity.OUTPUT_KIND` is `"tabular"` and
the `earthlens.earthlens.EarthLens` facade rejects an `aggregate=`
argument for it.

The archive has one CSV per (sensor, day) but no bbox index, so the
backend discovers active sensors in the bbox via the live JSON API, then
fetches each discovered sensor's per-day archive CSV over the date range.
Historical coverage is therefore limited to sensors currently reporting
in the bbox. Readings are crowdsourced from low-cost sensors and licensed
under the ODbL; every `download()` emits a `LicenseWarning`.

Public surface (re-exported from this package):

* `SensorCommunity` — the backend; instantiate with a date range, a
  bbox, and `variables=[pollutant, ...]`, then call
  `SensorCommunity.download`.
* `Catalog` — pydantic-backed loader for the bundled
  `sensor_community_data_catalog.yaml` pollutant dispatch table.
* `Pollutant` — one pollutant's dispatch row (`name`, `column`,
  `sensor_types`, `units`, `display_name`, `group`).
* `LicenseWarning` — emitted on every `download()` to flag the ODbL /
  low-cost-sensor quality caveat.
* `CATALOG_PATH` — path to the bundled pollutant YAML; monkey-patchable
  in tests.

Examples:
    - List the registered pollutants:

        ```python
        >>> from earthlens.sensor_community import Catalog
        >>> sorted(Catalog().pollutants)
        ['humidity', 'pm1', 'pm10', 'pm25', 'pressure', 'temperature']

        ```
"""

from __future__ import annotations

from earthlens.sensor_community._helpers import LicenseWarning
from earthlens.sensor_community.backend import SensorCommunity
from earthlens.sensor_community.catalog import CATALOG_PATH, Catalog, Pollutant

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "LicenseWarning",
    "Pollutant",
    "SensorCommunity",
]
