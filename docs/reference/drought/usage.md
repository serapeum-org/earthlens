# Drought indicators — usage

This page shows the three live transports in action.

## Pick a dataset

The catalog is sharded under `src/earthlens/drought/catalog/` —
`usdm.yaml`, `edo.yaml`, `gdo.yaml`, `speibase.yaml` — plus an
`_index.yaml` carrying every curated dataset id. Inspect from Python:

```python
from earthlens.drought import Catalog

cat = Catalog()
sorted(cat.datasets)[:5]
# ['edo-cdiad', 'edo-cdinx', 'edo-cdirc', 'edo-fpanv', 'edo-lfinx']

row = cat.get("usdm")
row.transport, row.output_kind, row.cadence
# ('usdm-geojson', 'vector', 'weekly')
```

An unknown dataset id raises `ValueError` with a sorted catalog and a
did-you-mean hint:

```python
>>> Catalog().get("usdmm")  # doctest: +SKIP
ValueError: 'usdmm' is not in the drought catalog. Known datasets: [...]. Did you mean 'usdm'?
```

## USDM — weekly polygon classes (vector)

The most common request: the latest weekly USDM polygons over a US bbox.
The `"usdm"` facade alias pre-binds `dataset="usdm"`:

```python
from earthlens import EarthLens

facade = EarthLens(
    data_source="usdm",
    start="2026-06-23", end="2026-06-23",
    variables=[],
    lat_lim=[30.0, 40.0],
    lon_lim=[-95.0, -85.0],
)
fc = facade.download()
fc.crs.to_epsg()        # 4326
sorted(fc["DM"].unique())  # [0, 1, 2, 3, 4]
fc["release_date"].iloc[0] # '2026-06-23'  (the Tuesday valid date)
```

The result is a
`pyramids.feature.collection.FeatureCollection` (a `GeoDataFrame`) in
EPSG:4326. A multi-week range snaps to one Tuesday per week and merges
into one `FeatureCollection` with a `release_date` column so you can
trace each polygon back to its weekly valid date.

Asking for `aggregate=` on USDM is rejected — drought-class polygons
have no gridded reduction:

```python
>>> facade.download(aggregate=object())  # doctest: +SKIP
NotImplementedError: Drought.download(aggregate=...) is not supported for the USDM (vector) transport: drought-class polygons have no gridded reduction. ...
```

## SPEIbase — global monthly raster

Pick a SPEI timescale (1, 3, 6, 12, 24, 48 months) and a bbox; the
backend downloads the per-scale NetCDF once, slices each requested month
through `pyramids.netcdf.NetCDF.subset`, and writes one GeoTIFF per
month under `path/`.

```python
from earthlens import EarthLens

facade = EarthLens(
    data_source="drought",
    dataset="speibase-12",
    start="2024-01-01", end="2024-03-31",
    variables=[],
    lat_lim=[30.0, 40.0],
    lon_lim=[-95.0, -85.0],
    path="speibase_out",
)
paths = facade.download()
# [Path('speibase_out/speibase-12_202401.tif'),
#  Path('speibase_out/speibase-12_202402.tif'),
#  Path('speibase_out/speibase-12_202403.tif')]
```

The downloaded NetCDF stays under `path/` between runs, so a second
`download()` for the same dataset reuses the cached `.nc` without
re-hitting `digital.csic.es`. Bump the catalog row's `endpoint` to point
at a newer SPEIbase release when one ships — no code change.

## EDO / GDO — Copernicus drought indicators (raster, pending)

EDO/GDO catalog rows resolve through the same facade — `dataset="edo-spaST"`
for SPI ERA5 short-term over Europe, `dataset="gdo-twsan"` for the GRACE
TWS anomaly over the globe, etc. The backend routes them via OGC WCS 2.0.0
with a `subset=time(...)` axis, which lands in pyramids as the temporal
extension of `pyramids.wcs.read_wcs` (the cross-repo `PY-A` task). Until
the pyramids release ships:

```python
>>> EarthLens(  # doctest: +SKIP
...     data_source="drought", dataset="edo-spaST",
...     start="2026-06-21", end="2026-06-21",
...     variables=[], lat_lim=[40.0, 50.0], lon_lim=[5.0, 15.0],
... ).download()
NotImplementedError: Drought.edo-wcs transport waits on the pyramids temporal `read_wcs` extension (PY-A). ...
```

The list of curated EDO/GDO ids lives in `src/earthlens/drought/catalog/edo.yaml`
and `gdo.yaml`. The indicator codes were scraped from the live Copernicus
WMS GetCapabilities on 2026-06-26 (see `planning/drought/captures/`) and
mirror the WCS coverage ids on the same MapServer.

## Aliases

Four facade keys point at the drought backend:

* `"drought"` — the canonical key; takes `dataset=` verbatim.
* `"usdm"` — pre-binds `dataset="usdm"` so the most common request needs
  no `dataset=` kwarg.
* `"edo"` / `"gdo"` — namespace aliases for the European / Global
  indicator families; the caller still names the indicator via
  `dataset="edo-spaST"` / `dataset="gdo-twsan"`.

## Date snapping at a glance

```python
from datetime import date
from earthlens.drought._helpers import snap_to_cadence

snap_to_cadence([date(2026, 6, 23)], "weekly")   # [date(2026, 6, 23)] — Tuesday valid date
snap_to_cadence([date(2026, 6, 15)], "10day")    # [date(2026, 6, 11)] — middle dekad
snap_to_cadence([date(2026, 6, 25)], "monthly")  # [date(2026, 6, 1)]
```

A range of dates collapses to one snapped period per release — so a
week-long USDM range yields one FeatureCollection per Tuesday valid date.

## Attributions logged on success

Every successful `download()` logs the per-source attribution as a
single info line (no `LicenseWarning`):

* **USDM**: "U.S. Drought Monitor — public-domain weekly composite
  produced by NDMC / UNL / USDA / NOAA. Cite the National Drought
  Mitigation Center."
* **EDO/GDO**: "Copernicus European/Global Drought Observatory (EMS) —
  free reuse with attribution to Copernicus EMS."
* **SPEIbase**: "CSIC Standardised Precipitation-Evapotranspiration
  Index database v2.10 (Vicente-Serrano et al.), CC-BY 4.0."
