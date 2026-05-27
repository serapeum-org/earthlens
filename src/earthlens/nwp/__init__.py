"""NWP backend — open numerical-weather-prediction forecasts.

One subpackage over the open NWP buckets (NOAA NODD, ECMWF Open Data,
DWD Open Data, with Météo-France / ECCC as follow-ons). Unlike the
observation-time backends, NWP is indexed by a **forecast time axis**
`(cycle_datetime_utc, forecast_step_hours)`: `start` / `end` select
the cycle date range and a `steps=` / `horizon=` kwarg picks the lead
times. The request is `variables = {model_key: [param, ...]}` and the
output is one bbox-cropped COG per `(cycle, step)`.

Herbie owns the GRIB2 `.idx` byte-range subsetting for the NOAA /
ECMWF models; earthlens contributes a thin per-centre adapter plus
direct modules (DWD HTTPS `.bz2`) for what Herbie does not cover. The
`[nwp]` extra pulls `herbie-data` + `ecmwf-opendata`; both SDKs (and
the cfgrib / eccodes stack Herbie's import chain needs) are imported
lazily, so this package imports without the extra installed.

Public surface (re-exported from this package):

* :class:`NWP` — the backend; instantiate with a date range, a bbox,
  and a `{model_key: [param, ...]}` mapping, then call
  :meth:`NWP.download`.
* :class:`Catalog` — pydantic-backed loader for the bundled
  `nwp_data_catalog.yaml`.
* :class:`NWPModel` — one curated model row (provider, cycles, backend,
  mirrors, band → selector map).
* :data:`CATALOG_PATH` — absolute path to the bundled catalog YAML;
  monkey-patchable to redirect the loader.
"""

from __future__ import annotations

from earthlens.nwp.backend import NWP
from earthlens.nwp.catalog import CATALOG_PATH, Catalog, NWPModel

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "NWP",
    "NWPModel",
]
