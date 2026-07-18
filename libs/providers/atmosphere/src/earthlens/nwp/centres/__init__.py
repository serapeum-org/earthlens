"""Per-centre GRIB2 fetchers for the NWP backend.

Each module here implements :class:`earthlens.nwp.centres.base._NWPCentre`
for one numerical-weather-prediction centre. `base` carries the
interface and the lazy :func:`resolve_centre` dispatch; the concrete
centres (`noaa`, `ecmwf`, `dwd`) are imported on demand so an unused
centre's optional SDK never has to be installed.
"""

from __future__ import annotations

from earthlens.nwp.centres.base import CENTRE_REGISTRY, _NWPCentre, resolve_centre

__all__ = ["CENTRE_REGISTRY", "_NWPCentre", "resolve_centre"]
