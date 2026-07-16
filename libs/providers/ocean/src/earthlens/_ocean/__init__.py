"""Backend table for the `earthlens-ocean` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 12 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'cmems': ('earthlens.cmems', 'CMEMS', 'cmems', {}),
    'nwm': ('earthlens.nwm', 'NWM', 'nwm', {}),
    'national-water-model': ('earthlens.nwm', 'NWM', 'nwm', {}),
    'usgs-water': ('earthlens.usgs_water', 'USGSWater', 'usgs-water', {}),
    'usgs-nwis': ('earthlens.usgs_water', 'USGSWater', 'usgs-water', {}),
    'nwis': ('earthlens.usgs_water', 'USGSWater', 'usgs-water', {}),
    'obis': ('earthlens.obis', 'OBIS', 'obis', {}),
    'argo': ('earthlens.argo', 'ARGO', 'argo', {}),
    'argo-floats': ('earthlens.argo', 'ARGO', 'argo', {}),
    'argopy': ('earthlens.argo', 'ARGO', 'argo', {}),
    'erddap': ('earthlens.erddap', 'ERDDAP', 'erddap', {}),
    'ioos': ('earthlens.erddap', 'ERDDAP', 'erddap', {}),
}
