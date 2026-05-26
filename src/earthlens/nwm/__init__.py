"""NOAA National Water Model backend (operational hydrologic output).

Fetches National Water Model NetCDF output from the unsigned
`noaa-nwm-pds` AWS bucket. NWM routes the land-surface water budget onto
the NHDPlus river network: `channel_rt` is per-reach streamflow (indexed
by `feature_id`, not a lat/lon grid), alongside gridded `land`,
`reservoir`, and `terrain_rt` products. The request follows the NWP
forecast axis — a configuration runs on UTC `cycles` and publishes
forecast `steps` (`fNNN`).

Public surface (re-exported from this package):

* :class:`NWM` — the backend; instantiate with a cycle-date window, a
  bbox, and a `{configuration: [product, ...]}` mapping, then call
  :meth:`NWM.download`.
* :class:`NWMCatalog` — loader for the bundled `nwm_data_catalog.yaml`.
* :class:`NWMConfig` — one configuration row (cycles / horizon / products
  / key template).
* :data:`CATALOG_PATH` — absolute path to the bundled config YAML.
* :data:`BUCKET` — the unsigned NWM bucket name.

The `[nwm]` extra pulls `boto3` (unsigned S3); it is imported lazily, so
the package imports without the extra installed. NWM output is native
NetCDF — :meth:`NWM.download` fetches the files and returns a
`pandas.DataFrame` inventory; decoding `channel_rt` streamflow into a
tidy table (via `xarray`) is a downstream follow-on.
"""

from __future__ import annotations

from earthlens.nwm.backend import BUCKET, NWM
from earthlens.nwm.catalog import CATALOG_PATH, NWMCatalog, NWMConfig

__all__ = [
    "BUCKET",
    "CATALOG_PATH",
    "NWM",
    "NWMCatalog",
    "NWMConfig",
]
