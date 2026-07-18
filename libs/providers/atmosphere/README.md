# earthlens-atmosphere

Weather, climate, air quality and solar/wind resource backends for earthlens.

Part of [earthlens](https://github.com/serapeum-org/earthlens). Ships 17 backends (airnow, chc, climate_indices, cmip6, drought, ecmwf, eea_aq, goes, nrel, nwp, openaq, pvgis, radar, s3, sensor_community, solar_wind_atlas, tropycal) and registers their data-source keys with the `EarthLens` facade via the `earthlens.backends` entry-point group.

Provider SDKs are extras, so `pip install earthlens-atmosphere` installs the backends without their dependencies; add a backend's SDK with `pip install earthlens-atmosphere[<backend>]`, or all of them with `[all]`.
