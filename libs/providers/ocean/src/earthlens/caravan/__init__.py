"""Caravan large-sample hydrology backend.

Caravan is an open community dataset of per-catchment daily **streamflow**,
**ERA5-Land** meteorological forcing, static catchment **attributes** and basin
**polygons**, published as static archives on Zenodo. This subpackage fetches
those archives and assembles the requested catchments into a
:class:`pandas.DataFrame`.

Its headline value is the **GRDC-Caravan** extension: the Global Runoff Data
Centre's raw portal has no API and forbids redistribution, but its openly
licensed stations are published here under CC-BY-4.0, so this is the legal,
scriptable route to open GRDC discharge.

Caravan is a **versioned historical archive, not a live feed** — releases land
every 4–12 months and the series lag the present by a year or more. For current
discharge use `earthlens.usgs_water` (US near-real-time) or GloFAS via
`earthlens.ecmwf`.

Public surface:

* :class:`Caravan` — the backend itself.
* :class:`Catalog` — the bundled extension / variable catalog, plus
  :class:`Extension`, :class:`Version`, :class:`ArchiveFile`, :class:`Source`
  and :class:`Variable` rows.
* :data:`CATALOG_PATH` / :func:`clear_catalog_cache` — the catalog file and its
  parse-cache control.
"""

from __future__ import annotations

from earthlens.caravan.backend import Caravan
from earthlens.caravan.catalog import (
    CATALOG_PATH,
    ArchiveFile,
    ArchiveFormat,
    Catalog,
    ColumnSet,
    Extension,
    Source,
    TimeseriesFormat,
    Variable,
    Version,
    clear_catalog_cache,
)

__all__ = [
    "ArchiveFile",
    "ArchiveFormat",
    "Caravan",
    "Catalog",
    "CATALOG_PATH",
    "ColumnSet",
    "clear_catalog_cache",
    "Extension",
    "Source",
    "TimeseriesFormat",
    "Variable",
    "Version",
]
