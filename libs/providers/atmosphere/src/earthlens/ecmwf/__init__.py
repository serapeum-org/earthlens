"""ECMWF / CADS backend (CDS + ADS + EWDS + ECDS + XDS).

Reaches all five CADS instances through one
:mod:`cdsapi` client and one Personal Access Token, routing each
dataset to its store via the catalog `endpoint`: the Climate Data
Store (C3S — ERA5, CARRA / CERRA, seasonal, CMIP5 / CORDEX, satellite
CDRs), the Atmosphere Data Store (CAMS — air quality, greenhouse
gases, composition), and the Early Warning Data Store (CEMS — GloFAS /
EFAS river discharge, fire danger). Downloads the store's NetCDF
(GRIB and zip-of-NetCDF are handled) and can aggregate it per window;
any dataset is also reachable by a raw request through the passthrough.

Public surface (re-exported from this package):

* :class:`ECMWF` — the backend itself; instantiate with a date range,
  a bbox, and a list of variable short codes, then call
  :meth:`ECMWF.download` to fetch every variable.
* :class:`Catalog` — pydantic-backed loader for the bundled multi-store
  catalog (the `catalog/` directory). Variables are addressed by the
  `(dataset, variable)` pair via :meth:`Catalog.get_variable`;
  :attr:`Catalog.available_datasets` is the per-store availability
  index and :meth:`Catalog.store_for` resolves a dataset's store.
* :class:`Dataset` — one CDS dataset's section inside the catalog
  (monthly variant + variables map).
* :class:`Variable` — one variable's metadata (CDS request name,
  NetCDF short name, raw ERA5 unit, pressure-level info).
* :class:`AuthenticationError` — raised when cdsapi cannot
  authenticate against CDS.
* :data:`ERA5_GRID_DEGREES` — ERA5 native grid spacing (0.125°),
  used by :meth:`ECMWF._create_grid` to snap user bboxes.
* :data:`CATALOG_PATH` — absolute path to the bundled YAML catalog;
  monkey-patchable to redirect the loader.

The catalog YAML ships with this package as data, loaded by
:class:`Catalog` from `Path(__file__).parent`.

Examples:
    - List datasets and look up a variable by `(dataset, code)`:

        ```python
        >>> from earthlens.ecmwf import Catalog
        >>> cat = Catalog()
        >>> "reanalysis-era5-single-levels" in cat.datasets
        True
        >>> cat.get_variable(
        ...     "reanalysis-era5-single-levels", "2m-temperature"
        ... ).nc_variable
        't2m'

        ```
"""

from __future__ import annotations

from earthlens.ecmwf._helpers import CadsUnavailableError
from earthlens.ecmwf.backend import (
    ECMWF,
    ERA5_GRID_DEGREES,
    AuthenticationError,
)
from earthlens.ecmwf.catalog import CATALOG_PATH, Catalog, Dataset, Variable

__all__ = [
    "ECMWF",
    "Catalog",
    "Dataset",
    "Variable",
    "AuthenticationError",
    "CadsUnavailableError",
    "ERA5_GRID_DEGREES",
    "CATALOG_PATH",
]
