"""Backend table for the `earthlens-land` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 26 data-source keys.
#: Module path for the FABDEM backend, reused across its alias keys.
_FABDEM = "earthlens.fabdem"

BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'ghsl': ('earthlens.ghsl', 'GHSL', '', {}),
    'ghs': ('earthlens.ghsl', 'GHSL', '', {}),
    'ghsl:human-settlement': ('earthlens.ghsl', 'GHSL', '', {}),
    'worldpop': ('earthlens.worldpop', 'WorldPop', 'worldpop', {}),
    'world-pop': ('earthlens.worldpop', 'WorldPop', 'worldpop', {}),
    'gbif': ('earthlens.gbif', 'GBIF', 'gbif', {}),
    'wdpa': ('earthlens.wdpa', 'WDPA', '', {}),
    'protected-planet': ('earthlens.wdpa', 'WDPA', '', {}),
    'iucn': ('earthlens.iucn', 'IUCN', '', {}),
    'redlist': ('earthlens.iucn', 'IUCN', '', {}),
    'bathymetry': ('earthlens.bathymetry', 'Bathymetry', '', {}),
    'gebco': ('earthlens.bathymetry', 'Bathymetry', '', {}),
    'etopo': ('earthlens.bathymetry', 'Bathymetry', '', {}),
    'glaciers': ('earthlens.glaciers', 'Glaciers', '', {}),
    'rgi': ('earthlens.glaciers', 'Glaciers', '', {}),
    'glims': ('earthlens.glaciers', 'Glaciers', '', {}),
    'wgms': ('earthlens.glaciers', 'Glaciers', '', {}),
    'soilgrids': ('earthlens.soilgrids', 'SoilGrids', '', {}),
    'isric': ('earthlens.soilgrids', 'SoilGrids', '', {}),
    'dem': ('earthlens.dem', 'DEM', 's3', {}),
    'copernicus-dem': ('earthlens.dem', 'DEM', 's3', {}),
    'cop-dem': ('earthlens.dem', 'DEM', 's3', {}),
    'dem:elevation': ('earthlens.dem', 'DEM', 's3', {}),
    'fabdem': (_FABDEM, 'FABDEM', '', {}),
    'fab-dem': (_FABDEM, 'FABDEM', '', {}),
    'fabdem:bare-earth-dem': (_FABDEM, 'FABDEM', '', {}),
}
