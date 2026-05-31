# WorldPop — the optional WorldPopPy path & the no-xarray design

earthlens reaches WorldPop two ways. The **default** is the direct REST API
(`api="rest"`); an **optional** path (`api="worldpoppy"`) delegates the
download to the [WorldPopPy](https://github.com/lungoruscello/WorldPopPy)
SDK. Both produce identical localised GeoTIFFs and tables — the difference
is only *who fetches the bytes*.

## Why REST is the default

The WorldPopPy public API is `wp_raster(product_name, aoi, years) ->
xarray.DataArray`. earthlens follows the project rule that **`xarray` is a
[`pyramids`](https://github.com/Serapieum-of-alex/pyramids) competitor and is
never imported under `src/`** — all gridded I/O and GIS operations go through
pyramids. Consuming `wp_raster`'s `xarray.DataArray` return would violate
that, so the REST path (just `requests` + pyramids) is primary and needs no
optional dependency.

A guard test (`tests/worldpop/test_no_xarray.py`) asserts that no file under
`earthlens.worldpop` contains `import xarray`.

## How the optional path stays xarray-free

When `api="worldpoppy"`, earthlens:

1. lazily imports `wp_raster` and `get_cache_dir` from `worldpoppy`;
2. calls `wp_raster(product_name=…, aoi=<iso3 list>, years=…,
   download_dry_run=True)` — which **downloads the GeoTIFFs into the SDK's
   on-disk cache** — and **discards the returned `xarray.DataArray`**;
3. reads the cached `.tif` files from `get_cache_dir()` with pyramids and
   runs them through the same mosaic + crop + table pipeline as the REST
   path.

So the optional SDK is consumed only through its **file cache**, never its
in-memory xarray object.

## Install

```bash
pip install earthlens[worldpop]
```

WorldPopPy is **MPL-2.0** and requires **Python ≥3.10**. Without the extra,
`api="worldpoppy"` raises a friendly `ImportError` naming
`earthlens[worldpop]`; the default `api="rest"` keeps working with no extra.

## Usage

```python
from earthlens.earthlens import EarthLens

EarthLens(
    data_source="worldpop",
    variables=["pop"],
    aoi="COM",
    start="2020", end="2020", fmt="%Y",
    lat_lim=[-12.5, -11.3], lon_lim=[43.2, 44.6],
    path="out/",
    api="worldpoppy",          # opt into the SDK fetch
).download()
```

The cache location honours WorldPopPy's own `WORLDPOPPY_CACHE_DIR`
environment variable.

## When to use which

- **`api="rest"` (default)** — no extra dependency, full control over the
  sub-alias matrix, and the same output as the SDK. Use this unless you have
  a reason not to.
- **`api="worldpoppy"`** — if you already rely on WorldPopPy's cache
  elsewhere, or want its product-resolution logic. earthlens still localises
  with pyramids.
