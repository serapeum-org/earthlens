# JRC European flood hazard (EFHM) — introduction

earthlens ships a `jrc-flood` backend that fetches the **JRC European Flood
Hazard Map (EFHM)** — "River flood hazard maps for Europe and the Mediterranean
Basin" — subset to a requested bounding box and written as GeoTIFF. Each cell
value is **river-flood water depth in metres** for a chosen **return period**
(how rare the flood is: a 1-in-100-year event, etc.). The map is produced by the
JRC / Copernicus Emergency Management Service (CEMS).

This page orients the backend; for the walkthrough see [Usage](usage.md), the
return periods on the [Available datasets](datasets.md) page, and the rendered
API on the [Reference](jrc-flood.md) page.

## Global vs European coverage

There are two JRC flood-hazard products, and earthlens covers both — through
different backends:

- **Global** (`JRC/CEMS_GLOFAS/FloodHazard`, ~90 m, RP10–RP500) — already
  available through the [`gee`](../gee/introduction.md) backend as a curated catalog row
  (`JRC/CEMS_GLOFAS/FloodHazard/v2_1`). It covers Europe too. Use `gee` for the
  global product.
- **European EFHM** (this backend, ~90 m / 3 arc-second, 9 return periods) — a
  higher-fidelity river-flood map for Europe and the Mediterranean, published as
  whole-Europe GeoTIFFs on the JRC open-data server, **not** on Earth Engine.

## How it works

Each return period is one whole-Europe EPSG:4326 GeoTIFF of ~23 GB uncompressed,
so the backend **never reads it whole**. It opens the file lazily over GDAL's
`/vsicurl` (HTTP range requests), reads **only** the AOI's pixel window through
the [pyramids](https://github.com/serapeum-org/pyramids) GIS backend, rebuilds a
small cropped grid, and writes one GeoTIFF per return period. So a small AOI
transfers only kilobytes, not gigabytes. `download()` returns the written paths
(`list[Path]`); earthlens never imports a competing array stack.

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="jrc-flood",
    lat_lim=[51.8, 52.0],
    lon_lim=[4.8, 5.0],
    return_periods=[100],
    path="efhm_out",
).download()
# -> [Path('efhm_out/efhm_RP100.tif')]
```

## Two things that shape the backend

- **The request axis is `return_periods`, not `variables`.** The EFHM has a
  single `water_depth` band, so the backend is *facet-only*: select the flood
  rarity with `return_periods=` (ints, `"100"`, or `"RP100"`); there is no
  `variables=` / `dataset=` axis. It is static, so `aggregate=` is rejected.

- **A windowed read, not a whole-file download.** Only the AOI window is fetched
  over `/vsicurl`. An AOI outside the Europe / Mediterranean coverage raises a
  clear `ValueError` rather than writing an empty raster.

## Licence

The EFHM is **CC-BY-4.0** (permissive attribution) — no licence warning. Cite:
Dottori, F., Alfieri, L., Bianchi, A., Skoulikaris, C., Salamon, P. (2020),
*River flood hazard maps for Europe and the Mediterranean Basin region*, JRC /
Copernicus Emergency Management Service.
