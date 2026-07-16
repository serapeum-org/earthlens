# earthlens-hazards

Hazard, humanitarian and vector-basemap backends for earthlens.

Part of [earthlens](https://github.com/serapeum-org/earthlens). Ships 8 backends (admin, fdsn, firms, gdacs, hdx, osm, overture, risk_indicators) and registers their data-source keys with the `EarthLens` facade via the `earthlens.backends` entry-point group.

Provider SDKs are extras, so `pip install earthlens-hazards` installs the backends without their dependencies; add a backend's SDK with `pip install earthlens-hazards[<backend>]`, or all of them with `[all]`.
