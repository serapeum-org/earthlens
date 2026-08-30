"""Backend table for the `earthlens-hazards` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: Module path shared by the three `aqueduct` facade keys (canonical + aliases).
_AQUEDUCT_MODULE = "earthlens.aqueduct"

#: Module path shared by the three NSI facade keys (nsi / nfip / nfhl).
_NSI = "earthlens.nsi"

#: Module path for the JRC backend, reused across its alias keys.
_JRC = "earthlens.jrc"

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 38 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'fdsn': ('earthlens.fdsn', 'FDSN', 'fdsn', {}),
    'gdacs': ('earthlens.gdacs', 'GDACS', '', {}),
    # FLOPROS global flood-protection standards (Scussolini et al. 2016) — one
    # public NHESS supplement shapefile, CC-BY-3.0, no extra SDK.
    'flopros': ('earthlens.flopros', 'FLOPROS', '', {}),
    # WRI Aqueduct riverine flood-risk exposure (files.wri.org 2015 Analyzer);
    # public CC-BY-4.0, no extra SDK (core requests + pyramids).
    'aqueduct': (_AQUEDUCT_MODULE, 'Aqueduct', '', {}),
    'aqueduct-floods': (_AQUEDUCT_MODULE, 'Aqueduct', '', {}),
    'aqueduct-flood-risk': (_AQUEDUCT_MODULE, 'Aqueduct', '', {}),
    # HANZE historical European flood impacts — public Zenodo record
    # (CC-BY-4.0), no extra SDK: the deps are core.
    'hanze': ('earthlens.hanze', 'HANZE', '', {}),
    # FLODIS global observed flood footprints <-> impacts — public Zenodo record
    # 8123096 (CC-BY-4.0), no extra SDK: HttpClient + pandas are core. The global
    # companion to hanze; sibling of emdat (EM-DAT/GDIS/GFD-derived).
    'flodis': ('earthlens.flodis', 'FLODIS', '', {}),
    # The extras hint covers the gdis:* sources, which need earthaccess. The
    # emdat:events source is anonymous HTTP and needs no extra; the hint is
    # per key, not per dataset, so it is stated once here.
    'emdat': ('earthlens.emdat', 'EMDAT', 'emdat', {}),
    'gdis': ('earthlens.emdat', 'EMDAT', 'emdat', {}),
    'hdx': ('earthlens.hdx', 'HDX', 'hdx', {}),
    'overture': ('earthlens.overture', 'Overture', 'overture', {}),
    'firms': ('earthlens.firms', 'FIRMS', '', {}),
    'jrc-flood': (_JRC, 'JRC', '', {}),
    'efhm': (_JRC, 'JRC', '', {}),
    'jrc-flood-hazard': (_JRC, 'JRC', '', {}),
    'european-flood-hazard': (_JRC, 'JRC', '', {}),
    # Sea-level (Total Water Level) probabilistic forecasts — same JRC backend;
    # dataset='sea_level' selects the family (product / representation pick the row).
    'sea-level-forecast': (_JRC, 'JRC', '', {'dataset': 'sea_level'}),
    'jrc-sea-level': (_JRC, 'JRC', '', {'dataset': 'sea_level'}),
    'coastal-forecast': (
        _JRC,
        'JRC',
        '',
        {'dataset': 'sea_level', 'product': 'subseasonal', 'representation': 'coastal'},
    ),
    'twl-forecast': (_JRC, 'JRC', '', {'dataset': 'sea_level'}),
    'risk-indicators': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'thinkhazard': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'inform': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'gfw': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'global-forest-watch': ('earthlens.risk_indicators', 'RiskIndicators', '', {}),
    'osm': ('earthlens.osm', 'OSM', 'osm', {}),
    'openstreetmap': ('earthlens.osm', 'OSM', 'osm', {}),
    'overpass': ('earthlens.osm', 'OSM', 'osm', {}),
    'ohsome': ('earthlens.osm', 'OSM', 'osm', {}),
    # NSI — US object-level flood exposure & loss over three keyless sources.
    # `nsi` defaults to source='structures'; the source-pinning aliases carry a
    # default_kwargs so `EarthLens("nfip")` does not silently fall back to it.
    'nsi': (_NSI, 'NSI', '', {}),
    'nfip': (_NSI, 'NSI', '', {'source': 'nfip'}),
    'nfhl': (_NSI, 'NSI', '', {'source': 'nfhl'}),
    'admin': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'admin-boundaries': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'geoboundaries': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'natural-earth': ('earthlens.admin', 'AdminBoundaries', '', {}),
    'tiger': ('earthlens.admin', 'AdminBoundaries', '', {}),
}
