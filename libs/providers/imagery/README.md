# earthlens-imagery

Satellite platform, sar and eo catalog backends for earthlens.

Part of [earthlens](https://github.com/serapeum-org/earthlens). Ships 8 backends (asf, earthdata, eumetsat, gee, jaxa, openeo, sentinel_hub, stac) and registers their data-source keys with the `EarthLens` facade via the `earthlens.backends` entry-point group.

Provider SDKs are extras, so `pip install earthlens-imagery` installs the backends without their dependencies; add a backend's SDK with `pip install earthlens-imagery[<backend>]`, or all of them with `[all]`.
