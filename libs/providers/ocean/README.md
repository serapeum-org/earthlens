# earthlens-ocean

Ocean, freshwater and marine-life backends for earthlens.

Part of [earthlens](https://github.com/serapeum-org/earthlens). Ships 6 backends (argo, cmems, erddap, nwm, obis, usgs_water) and registers their data-source keys with the `EarthLens` facade via the `earthlens.backends` entry-point group.

Provider SDKs are extras, so `pip install earthlens-ocean` installs the backends without their dependencies; add a backend's SDK with `pip install earthlens-ocean[<backend>]`, or all of them with `[all]`.
