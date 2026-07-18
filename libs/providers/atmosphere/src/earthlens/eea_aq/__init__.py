"""EEA (European Environment Agency) ground-station air-quality backend.

Wraps the `airbase` client over the EEA download service and returns
reference-grade European monitor observations as a long-format
`pandas.DataFrame` (one row per measurement), the same `tabular` shape as
`earthlens.openaq`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `EEA_AQ.OUTPUT_KIND` is `"tabular"` and the
`earthlens.earthlens.core.EarthLens` facade rejects an `aggregate=` argument
for it.

The EEA service is queried per **country** (ISO2) and delivers Parquet.
The backend maps the request bbox to the reporting countries that
intersect it (or an explicit `country=`), picks the dataset era(s)
(`Historical` / `Verified` / `Unverified`) spanning the requested years,
and reshapes the downloaded Parquet into the long schema. Results are
country-granular and carry no `lat` / `lon` (see the backend docstring).
`airbase` (the `[eea_aq]` extra) is imported lazily.

Public surface (re-exported from this package):

* `EEA_AQ` — the backend; instantiate with a date range, a bbox (or
  `country=`), and `variables=[pollutant, ...]`, then call
  `EEA_AQ.download`.
* `Catalog` — pydantic-backed loader for the bundled
  `eea_aq_data_catalog.yaml` pollutant dispatch table.
* `Pollutant` — one pollutant's dispatch row (`name`, `poll`, `code`,
  `units`, `display_name`, `group`).
* `CATALOG_PATH` — path to the bundled pollutant YAML; monkey-patchable
  in tests.

Examples:
    - List the registered pollutants:

        ```python
        >>> from earthlens.eea_aq import Catalog
        >>> sorted(Catalog().pollutants)
        ['co', 'no2', 'o3', 'pm10', 'pm25', 'so2']

        ```
"""

from __future__ import annotations

from earthlens.eea_aq.backend import EEA_AQ
from earthlens.eea_aq.catalog import CATALOG_PATH, Catalog, Pollutant

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "EEA_AQ",
    "Pollutant",
]
