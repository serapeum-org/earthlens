# JRC hazards (EFHM + sea-level forecasts) — introduction

earthlens ships one **`earthlens.jrc`** backend for the JRC / Copernicus
Emergency Management Service (CEMS) hazard products. A single class (`JRC`)
serves every JRC dataset, selected by dataset and dispatched internally on the
catalog row's `kind` — the same "pick a dataset, the backend routes it" shape as
the [`ecmwf`](../ecmwf/introduction.md) backend's per-store `endpoint`:

| Facade key(s) | Product | `kind` | Output |
|---|---|---|---|
| `efhm` / `jrc-flood` / `jrc-flood-hazard` / `european-flood-hazard` | European Flood Hazard Map (river-flood depth per return period) | `flood_hazard_raster` | `list[Path]` GeoTIFF |
| `sea-level-forecast` / `jrc-sea-level` / `twl-forecast` | Probabilistic Total Water Level (TWL) forecasts (gridded) | `sea_level_gridded` | `list[Path]` GeoTIFF |
| `coastal-forecast` | Subseasonal coastal per-country summary | `sea_level_coastal` | `pandas.DataFrame` |

For the walkthrough see [Usage](usage.md), the datasets on the
[Available datasets](datasets.md) page, and the rendered API on the
[Reference](jrc.md) page.

## The European Flood Hazard Map (EFHM)

The EFHM is "River flood hazard maps for Europe and the Mediterranean Basin":
each cell is **river-flood water depth in metres** for a chosen **return period**
(how rare the flood is — a 1-in-100-year event, etc.). Each return period is one
whole-Europe EPSG:4326 GeoTIFF of ~23 GB uncompressed, so the backend **never
reads it whole**: it opens the file lazily over GDAL's `/vsicurl` (HTTP range
requests), reads only the AOI's pixel window through the
[pyramids](https://github.com/serapeum-org/pyramids) GIS backend, and writes one
GeoTIFF per return period. A small AOI transfers kilobytes, not gigabytes.

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="efhm",
    lat_lim=[51.8, 52.0],
    lon_lim=[4.8, 5.0],
    return_periods=[100],
    path="efhm_out",
).download()
# -> [Path('efhm_out/efhm_RP100.tif')]
```

The request axis is `return_periods` (ints, `"100"`, or `"RP100"`); the EFHM is
static, so `aggregate=` is rejected. An AOI outside the Europe / Mediterranean
coverage raises a clear `ValueError`.

There are two JRC flood-hazard products, and earthlens covers both: the **global**
map (`JRC/CEMS_GLOFAS/FloodHazard/v2_1`, ~90 m) is a curated row of the
[`gee`](../gee/introduction.md) backend; the higher-fidelity **European EFHM** is
this backend.

## The sea-level (Total Water Level) forecasts

The JRC also produces probabilistic, data-driven **sea-level forecasts** — storm
surge + tide + wave-derived coastal **Total Water Level (TWL)** — the coastal /
storm-surge counterpart to the river-flood EFHM. Two products, both **global
0.25°** NetCDF-4:

- **medium-term** — issued **twice daily**, 15-day horizon.
- **subseasonal** — issued **weekly**, ~46-day horizon, with a small **global
  per-country coastal-summary CSV** alongside the gridded cube.

A request selects a `product`, an optional `reference_time` (default `"latest"`,
which resolves the newest complete forecast cycle), and — for the gridded product
— a bounding box and a `field` (default `TWL75`, the 75th-percentile TWL). The
gridded cube is read the same windowed way as the EFHM but through
`pyramids.netcdf.NetCDF`, and written as one **multi-band GeoTIFF** (one band per
forecast time step).

```python
from earthlens.core import EarthLens

# gridded medium-term TWL forecast, latest cycle, cropped to the North Sea
paths = EarthLens(
    data_source="jrc:sea-level-forecast",
    product="medium_term",
    lat_lim=[51.0, 53.0],
    lon_lim=[3.0, 5.0],
    path="twl_out",
).download()

# subseasonal global coastal summary -> a pandas.DataFrame
summary = EarthLens(data_source="jrc:coastal-forecast").download()
```

The forecasts are read only over the AOI window (`/vsicurl`), so a small area
transfers little of the 13–38 GB cube. Because a forecast cycle is chosen by
`reference_time` (not a `start`/`end` scan) and carries no reducible time axis,
`aggregate=` is rejected.

## Licence

Every JRC product here is **CC-BY-4.0** (permissive attribution) — no licence
warning. Cite the EFHM as Dottori, F., Alfieri, L., Bianchi, A., Skoulikaris, C.,
Salamon, P. (2020), *River flood hazard maps for Europe and the Mediterranean
Basin region*, JRC / CEMS. The sea-level forecasts follow the group's 2024
methodology paper (full citation + DOI pending confirmation with the producers).
