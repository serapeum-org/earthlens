"""Backend table for the `earthlens-atmosphere` provider distribution.

Published to the facade through the `earthlens.backends` entry-point group
and merged into `EarthLens.DataSources` by
`earthlens._backends.discover_backends`.

Import-light by contract: this module holds names, never the backends
themselves, so resolving the entry point costs no provider SDK import.
"""

from __future__ import annotations

__all__ = ["BACKENDS"]

#: `key -> (module, class_name, extras_hint, default_kwargs)` for this
#: distribution's 36 data-source keys.
BACKENDS: dict[str, tuple[str, str, str, dict[str, object]]] = {
    'chc': ('earthlens.chc', 'CHIRPS', '', {}),
    'chirps': ('earthlens.chc', 'CHIRPS', '', {}),
    'amazon-s3': ('earthlens.s3', 'S3', 's3', {}),
    'cmip6': ('earthlens.cmip6', 'CMIP6', '', {}),
    'pangeo-cmip6': ('earthlens.cmip6', 'CMIP6', '', {}),
    'climate-projections': ('earthlens.cmip6', 'CMIP6', '', {}),
    'ecmwf': ('earthlens.ecmwf', 'ECMWF', 'ecmwf', {}),
    'goes': ('earthlens.goes', 'GOES', 's3', {}),
    'openaq': ('earthlens.openaq', 'OpenAQ', '', {}),
    'airnow': ('earthlens.airnow', 'AirNow', '', {}),
    'eea-aq': ('earthlens.eea_aq', 'EEA_AQ', 'eea_aq', {}),
    'sensor-community': ('earthlens.sensor_community', 'SensorCommunity', '', {}),
    'tropycal': ('earthlens.tropycal', 'TropicalCyclone', 'tropycal', {}),
    'nwp': ('earthlens.nwp', 'NWP', 'nwp', {}),
    'radar': ('earthlens.radar', 'Radar', 'radar', {}),
    'nexrad': ('earthlens.radar', 'Radar', 'radar', {}),
    'solar-wind-atlas': ('earthlens.solar_wind_atlas', 'SolarWindAtlas', '', {}),
    'global-solar-atlas': ('earthlens.solar_wind_atlas', 'SolarWindAtlas', '', {}),
    'global-wind-atlas': ('earthlens.solar_wind_atlas', 'SolarWindAtlas', '', {}),
    'gsa': ('earthlens.solar_wind_atlas', 'SolarWindAtlas', '', {}),
    'gwa': ('earthlens.solar_wind_atlas', 'SolarWindAtlas', '', {}),
    'pvgis': ('earthlens.pvgis', 'PVGIS', '', {}),
    'solar-pv': ('earthlens.pvgis', 'PVGIS', '', {}),
    'climate-indices': ('earthlens.climate_indices', 'ClimateIndices', '', {}),
    'climate_indices': ('earthlens.climate_indices', 'ClimateIndices', '', {}),
    'teleconnections': ('earthlens.climate_indices', 'ClimateIndices', '', {}),
    'nrel': ('earthlens.nrel', 'NREL', '', {}),
    'nsrdb': ('earthlens.nrel', 'NREL', '', {'product': 'nsrdb-psm3'}),
    'wind-toolkit': ('earthlens.nrel', 'NREL', '', {'product': 'wtk'}),
    'drought': ('earthlens.drought', 'Drought', '', {}),
    'mswep': ('earthlens.mswep', 'MSWEP', 'mswep', {}),
    'mswx': ('earthlens.mswep', 'MSWEP', 'mswep', {'product': 'mswx'}),
    'gloh2o': ('earthlens.mswep', 'MSWEP', 'mswep', {}),
    'usdm': ('earthlens.drought', 'Drought', '', {}),
    'edo': ('earthlens.drought', 'Drought', '', {}),
    'gdo': ('earthlens.drought', 'Drought', '', {}),
}
