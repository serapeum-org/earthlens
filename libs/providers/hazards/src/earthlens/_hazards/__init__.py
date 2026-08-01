"""Backend table for the `earthlens-hazards` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 21 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'fdsn': ('earthlens.fdsn', 'FDSN', 'fdsn', {}),
    'gdacs': ('earthlens.gdacs', 'GDACS', '', {}),
    # The extras hint covers the gdis:* sources, which need earthaccess. The
    # emdat:events source is anonymous HTTP and needs no extra; the hint is
    # per key, not per dataset, so it is stated once here.
    'emdat': ('earthlens.emdat', 'EMDAT', 'emdat', {}),
    'gdis': ('earthlens.emdat', 'EMDAT', 'emdat', {}),
    'hdx': ('earthlens.hdx', 'HDX', 'hdx', {}),
    'overture': ('earthlens.overture', 'Overture', 'overture', {}),
    'firms': ('earthlens.firms', 'FIRMS', '', {}),
    'risk-indicators': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'thinkhazard': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'inform': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'gfw': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'global-forest-watch': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'osm': ('earthlens.osm', 'OSM', 'osm', {}),
    'openstreetmap': ('earthlens.osm', 'OSM', 'osm', {}),
    'overpass': ('earthlens.osm', 'OSM', 'osm', {}),
    'ohsome': ('earthlens.osm', 'OSM', 'osm', {}),
    'admin': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'admin-boundaries': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'geoboundaries': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'natural-earth': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'tiger': ('earthlens.admin', 'AdminBoundaries', '', {}),
}
