# Solar & Wind Atlas — introduction

<img src="../../_images/logos/solar_wind_atlas.svg" alt="Global Solar Atlas / Global Wind Atlas (World Bank/ESMAP) logo" height="60">

earthlens ships a single `solar-wind-atlas` backend that fetches **global
solar- and wind-resource climatology layers** — long-term-average grids of
irradiation, PV potential, wind speed and related quantities — subset to a
requested bounding box and written as GeoTIFF. Two open data families are
covered:

- **[Global Solar Atlas](https://globalsolaratlas.info/)** (DTU / Solargis /
  ESMAP · World Bank) — solar-resource layers GHI, DNI, DIF, GTI plus PV
  output (PVOUT) and optimum PV tilt (OPTA).
- **[Global Wind Atlas](https://globalwindatlas.info/)** v3 (DTU / ESMAP) —
  mean wind speed at 100 m, Weibull shape parameter `k` at five heights,
  turbine capacity factor (IEC classes 1–3) and air density.

Both atlases are free and keyless (CC-BY-4.0). This page orients the backend.
For the hands-on walkthrough see [Usage](usage.md) and the layer ids on the
[Available layers](datasets.md) page; the rendered API is the
[Reference](solar_wind_atlas.md) page.

## How it works — two transports

Which transport a layer uses is set by how its atlas is hosted, not by a knob:

- **Global Wind Atlas layers are read *windowed*.** They are range-accessible
  Cloud-Optimized GeoTIFFs (COGs) on figshare, so the backend reads **only the
  bbox window** over a virtual `/vsicurl/` range read through the
  [pyramids](https://github.com/serapeum-org/pyramids) GIS backend
  (`Dataset.crop(bbox=)`). A small area transfers a few hundred KB, not the
  multi-GB global file.

- **Global Solar Atlas layers are *downloaded once, then cropped locally*.**
  They ship only as DEFLATE-compressed GeoTIFF ZIP archives, which cannot be
  range-windowed (decompressing any pixel needs the whole member). So the
  per-variable archive (~2.7 GB at 1 km) is downloaded once into a cache and the
  bbox window is read from the local member. The first fetch logs a one-time
  multi-GB-download warning; subsequent requests reuse the cache.

Either way, `download()` writes one cropped **GeoTIFF per requested layer** and
returns their paths (`list[Path]`). All raster I/O goes through pyramids —
earthlens never imports a competing array stack.

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="solar-wind-atlas",
    variables=["ghi", "wind_100m"],
    lat_lim=[55.0, 55.5],
    lon_lim=[12.0, 12.5],
    path="atlas_out",
).download()
# -> [Path('atlas_out/ghi.tif'), Path('atlas_out/wind_100m.tif')]
```

## Two things that shape the backend

- **The layers are static — there is no time axis.** Each layer is a single
  long-term-average climatology grid, not a time series, so `download()` has
  nothing to reduce over time. The `EarthLens` facade forwards `aggregate=` to
  raster backends, so this backend explicitly rejects a non-`None` `aggregate=`
  with `NotImplementedError`.

- **A request is a bbox subset.** The full atlas grids are global; this backend
  returns only the bbox you ask for — read windowed for wind, cropped from the
  cached download for solar.

## Attribution

Both atlases are CC-BY-4.0; cite them when you use the data (the backend also
logs the attribution once per atlas fetched):

- **Global Solar Atlas** — Global Solar Atlas 2.0, © The World Bank, Solar
  resource data: Solargis.
- **Global Wind Atlas** — Global Wind Atlas 3.0, © Technical University of
  Denmark (DTU) / World Bank (ESMAP).
