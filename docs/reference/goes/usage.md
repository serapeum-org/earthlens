# NOAA GOES-R ABI — usage

## The request shape

A GOES request is three-axis: a **satellite** (which bucket), a **product**
(`dataset=`, an ABI product family), and a **domain** (`domain=`, the scan
sector). GOES is sub-hourly, so give `start` / `end` a **time** for a tight
window (a bare date spans the whole UTC day).

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="goes",
    dataset="abi-l2-mcmip",              # Cloud & Moisture Imagery (16 bands)
    satellite="east",                    # -> noaa-goes19
    domain="C",                          # CONUS (5-min)
    start="2026-07-03 12:00",
    end="2026-07-03 12:30",
    fmt="%Y-%m-%d %H:%M",
    path="./goes_out",
)
paths = lens.download()                  # -> list[pathlib.Path] of raw NetCDF
```

You can also drive the backend directly:

```python
from earthlens.goes import GOES

goes = GOES(
    start="2026-07-03 12:00", end="2026-07-03 12:10",
    lat_lim=[-90, 90], lon_lim=[-180, 180],   # context only — no server subset
    dataset="abi-l1b-rad",                     # radiances (one file per channel)
    variables=["C02", "C13"],                  # -> only the C02 / C13 granules
    satellite="east", domain="C",
    fmt="%Y-%m-%d %H:%M",
    path="./goes_out",
)
paths = goes.download()
```

## Keyword arguments

| Argument | Meaning |
|----------|---------|
| `dataset` | An ABI product-family key from the catalog (`"abi-l2-mcmip"`, `"abi-l1b-rad"`, `"abi-l2-aod"`, …). Default `"abi-l2-mcmip"`. |
| `satellite` | `"east"` / `"west"` (current operational role) or `"16"` / `"18"` / `"19"` (explicit satellite / archive). Default `"east"`. |
| `domain` | `"C"` CONUS / `"F"` Full Disk / `"M1"` / `"M2"` Mesoscale. `None` (default) uses the product's `default_domain`. |
| `variables` | For a **band-split** product (`abi-l1b-rad`, `abi-l2-cmip`), the ABI channels to fetch (`["C02", "C13"]`) — selects which granule files. For the combined `abi-l2-mcmip` it is informational (the whole multi-band granule is fetched). `None` fetches every granule. |
| `start` / `end` | The inclusive scan-time window. Give a time (`"2026-07-03 12:00"` with `fmt="%Y-%m-%d %H:%M"`) for a tight window; a bare date spans the whole UTC day. |
| `lat_lim` / `lon_lim` | Bounding box, captured for context only — S3 serves whole granules, so there is no server-side spatial subset. Crop downstream with pyramids. |
| `path` | Output directory for the fetched NetCDF granules. |

Channel selectors are lenient: `"C02"`, `"c2"`, `"2"`, and `"CMI_C02"` all
resolve to the canonical ABI channel `C02`.

## What you get back

`download()` returns a `list[pathlib.Path]` — one raw NetCDF granule per
in-window scan (and, for a band-split product, per requested channel). The
files keep their upstream ABI names
(`OR_ABI-L2-MCMIPC-M6_G19_s…_e…_c….nc`), so the scan-start / end / created
timestamps and the satellite are visible at a glance. An hour whose S3 prefix
lists nothing (not yet published, or an outage) is **logged** and skipped —
never silently dropped.

## Decoding the granules downstream

The granules are raw geostationary NetCDF. Read and reproject them with
pyramids (which georeferences the ABI scan-angle grid from the CF
`goes_imager_projection` grid-mapping):

```python
from pyramids.netcdf import NetCDF

nc = NetCDF.read_file(str(paths[0]))
cmi = nc.get_variable("CMI_C13").to_crs(4326)   # warp to WGS84
cmi.to_file("goes_c13_wgs84.tif")
```

or feed them to [`satpy`](https://satpy.readthedocs.io/) for RGB composites.

## See also

* [Introduction](introduction.md) — satellites, products, domains, sizes.
* [Products & domains](datasets.md) — the curated catalog.
* [API reference](goes.md) — the rendered module API.
