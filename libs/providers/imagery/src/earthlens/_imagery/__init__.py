"""Backend table for the `earthlens-imagery` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 29 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'asf': ('earthlens.asf', 'ASF', 'asf', {}),
    'alaska-satellite-facility': ('earthlens.asf', 'ASF', 'asf', {}),
    'asf:insar': ('earthlens.asf', 'ASF', 'asf', {}),
    'earthdata': ('earthlens.earthdata', 'Earthdata', 'earthdata', {}),
    'eumetsat': ('earthlens.eumetsat', 'EUMETSAT', 'eumetsat', {}),
    'gee': ('earthlens.gee', 'GEE', 'gee', {}),
    'google-earth-engine': ('earthlens.gee', 'GEE', 'gee', {}),
    'openeo': ('earthlens.openeo', 'OpenEO', 'openeo', {}),
    'sentinel-hub': ('earthlens.sentinel_hub', 'SentinelHub', 'sentinel-hub', {}),
    'sentinelhub': ('earthlens.sentinel_hub', 'SentinelHub', 'sentinel-hub', {}),
    'stac': ('earthlens.stac', 'STAC', 'stac', {}),
    'planetary-computer': (
        'earthlens.stac',
        'STAC',
        'stac',
        {'endpoint': 'planetary-computer'},
    ),
    'earth-search': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'earth-search'}),
    'cdse': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'cdse'}),
    'deafrica': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'deafrica'}),
    'digital-earth-africa': (
        'earthlens.stac',
        'STAC',
        'stac',
        {'endpoint': 'deafrica'},
    ),
    'dea': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'dea'}),
    'digital-earth-australia': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'dea'}),
    'veda': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'veda'}),
    'usgs-landsat': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'usgs-landsat'}),
    'landsat': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'usgs-landsat'}),
    'bdc': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'bdc'}),
    'brazil-data-cube': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'bdc'}),
    'eodc': ('earthlens.stac', 'STAC', 'stac', {'endpoint': 'eodc'}),
    'jaxa': ('earthlens.jaxa', 'JAXA', 'jaxa', {}),
    'jaxa-earth': ('earthlens.jaxa', 'JAXA', 'jaxa', {}),
    'g-portal': ('earthlens.jaxa', 'JAXA', 'jaxa', {}),
    'ptree': ('earthlens.jaxa', 'JAXA', '', {}),
    'himawari': ('earthlens.jaxa', 'JAXA', '', {}),
}
