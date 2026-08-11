"""FLODIS observed-flood footprints <-> impacts backend.

Fetches the FLODIS dataset (Mester, Frieler & Schewe, PIK; Sci Data 10, 482,
2023) from its pinned static Zenodo record, and returns per-event impact records
as a :class:`pandas.DataFrame`. FLODIS is the observed hazard-footprint -> impact
bridge: it links EM-DAT fatalities + economic damages (`dataset="damages"`) and
IDMC displacements (`dataset="displacement"`) to Global Flood Database satellite
flood footprints, adding per-event affected population, GDP and
critical-infrastructure counts. The global companion to the European `hanze`
backend, and to the raw impact tables in `emdat`.

FLODIS carries the join keys — `disasterno` (EM-DAT) on the damages table,
`GID_1` / `GID_2` (GADM) on the displacement table — but does **not** re-fetch the
footprints: the GDIS geometry comes from the shipped `emdat` backend and the GFD
extents from the shipped `gee` backend (`GLOBAL_FLOOD_DB/MODIS_EVENTS/V1`), joined
on those keys.

This is a `tabular` backend: the result is a table of per-event impact rows, not
a gridded array, so the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument.

FLODIS needs **no credentials** — the Zenodo record is public (CC-BY-4.0) — so
there is no auth class and no `[flodis]` extra: the only dependencies (HttpClient,
pandas) are core.

Public surface (re-exported from this package):

* :class:`FLODIS` — the backend; instantiate with `dataset=` and optional
  `country=` / `gid=` / date filters, then call :meth:`FLODIS.download`.
* :class:`Catalog` — loader for the bundled `flodis_data_catalog.yaml`.
* :class:`ZenodoRecord` / :class:`FlodisDataset` — the catalog's frozen row
  models.
* :data:`CATALOG_PATH` — path to the bundled catalog YAML; monkey-patchable in
  tests.

Examples:
    - List the selectable tables:

        ```python
        >>> from earthlens.flodis import Catalog
        >>> Catalog().tables()
        ['damages', 'displacement']

        ```
"""

from __future__ import annotations

from earthlens.flodis.backend import FLODIS
from earthlens.flodis.catalog import (
    CATALOG_PATH,
    Catalog,
    FlodisDataset,
    ZenodoRecord,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "FLODIS",
    "FlodisDataset",
    "ZenodoRecord",
]
