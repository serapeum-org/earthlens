"""Backend table for the `earthlens-ocean` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: The caravan backend's module path, named once because three keys share it.
_CARAVAN = "earthlens.caravan"

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 15 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    # No extras hint: Caravan reads static Zenodo archives over plain HTTP, so
    # it needs no SDK beyond the core dependencies.
    'caravan': (_CARAVAN, 'Caravan', '', {}),
    # GRDC-Caravan is the reason this backend exists - the legal, scriptable
    # route to open GRDC discharge - so it is reachable under its own name.
    'caravan-grdc': (_CARAVAN, 'Caravan', '', {'dataset': 'grdc'}),
    'grdc-caravan': (_CARAVAN, 'Caravan', '', {'dataset': 'grdc'}),
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
