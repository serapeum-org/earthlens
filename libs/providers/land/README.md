# earthlens-land

Terrain, elevation, soil, ecology and population backends for earthlens.

Part of [earthlens](https://github.com/serapeum-org/earthlens). Ships 9 backends (bathymetry, dem, gbif, ghsl, glaciers, iucn, soilgrids, wdpa, worldpop) and registers their data-source keys with the `EarthLens` facade via the `earthlens.backends` entry-point group.

Provider SDKs are extras, so `pip install earthlens-land` installs the backends without their dependencies; add a backend's SDK with `pip install earthlens-land[<backend>]`, or all of them with `[all]`.
