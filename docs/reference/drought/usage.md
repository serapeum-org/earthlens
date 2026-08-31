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
# ['edo-cdiad', 'edo-cdirc', 'edo-lfinx-lgs', 'edo-msfTS', 'edo-rdria']

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
The four drought-facade keys (`drought` / `usdm` / `edo` / `gdo`) are
discoverability aliases — all resolve to the same backend and all
require an explicit `dataset=`:

```python
from earthlens.core import EarthLens

facade = EarthLens(
    data_source="usdm",
    start="2026-06-23", end="2026-06-23",
    variables=[],
    lat_lim=[30.0, 40.0],
    lon_lim=[-95.0, -85.0],
    dataset="usdm",
)
fc = facade.download()
fc.crs.to_epsg()        # 4326
sorted(fc["DM"].unique())  # [0, 1, 2, 3, 4]
fc["release_date"].iloc[0] # '2026-06-16' if queried on/before Thu 2026-06-25;
                           # '2026-06-23' once that Thursday release rolls out.
```

The result is a
`pyramids.feature.collection.FeatureCollection` (a `GeoDataFrame`) in
EPSG:4326. A multi-week range snaps to one Tuesday per week and merges
into one `FeatureCollection` with a `release_date` column so you can
trace each polygon back to its weekly valid date. The walk-back rule
fires when the snapped Tuesday's composite has not yet been released
(its release Thursday is still in the future) — historical queries
always land on the requested Tuesday.

Asking for `aggregate=` is rejected on every drought transport — the USDM
polygons have no gridded reduction, and the raster routes' cross-period stack
reducer is not wired yet. The refusal comes from the shared gate, so the
message names the backend and its output kind:

```python
>>> facade.download(aggregate=object())  # doctest: +SKIP
NotImplementedError: aggregate= is not supported by Drought (OUTPUT_KIND='vector'): no drought transport reduces across dates today. ...
```

### Capping the result

`limit=` bounds the polygons a USDM request returns. It is applied as each
week's GeoJSON arrives — after the bbox clip, so it counts rows you will
actually receive — which means a week past the cap is never downloaded:

```python
>>> facade.download(limit=500)  # doctest: +SKIP
```

It applies to the vector (USDM) transport only. The raster transports write
files, which a row cap cannot describe, so passing `limit=` to one of those
raises `ValueError` rather than being quietly ignored.

## SPEIbase — global monthly raster

Pick a SPEI timescale (1, 3, 6, 12, 24, 48 months) and a bbox; the
backend downloads the per-scale NetCDF once, slices each requested month
through `pyramids.netcdf.NetCDF.subset`, and writes one GeoTIFF per
month under `path/`.

```python
from earthlens.core import EarthLens

facade = EarthLens(
    data_source="drought",
    dataset="speibase-12",
    start="2023-01-01", end="2023-03-31",
    variables=[],
    lat_lim=[30.0, 40.0],
    lon_lim=[-95.0, -85.0],
    path="speibase_out",
)
paths = facade.download()
# [Path('speibase_out/speibase-12_202301.tif'),
#  Path('speibase_out/speibase-12_202302.tif'),
#  Path('speibase_out/speibase-12_202303.tif')]
```

The last usable month depends on the timescale — a k-month SPEI needs k
months to accumulate, so `speibase-12` currently ends 2023-12. The
backend reads the axis from the file and raises a clear error for an
out-of-range month.

The downloaded NetCDF stays under `path/` between runs, so a second
`download()` for the same dataset reuses the cached `.nc` without
re-hitting `digital.csic.es`. The cache file name embeds a hash of the
row's `endpoint`, so bumping the catalog row to a newer SPEIbase release
when one ships fetches the new file fresh (the stale cache is ignored) — a
pure YAML edit, no code change and no manual cache cleanup.

## EDO / GDO — Copernicus drought indicators (raster)

EDO/GDO catalog rows resolve through the same facade — `dataset="edo-spaST"`
for SPI ERA5 short-term, `dataset="gdo-smand"` for the ensemble soil-moisture
anomaly, etc. The backend builds the Copernicus `GetCoverage` URL by hand
(`TIME` + the row's `SELECTED_TIMESCALE` + a `SUBSET=Long/Lat` bbox),
streams the GeoTIFF, and opens it through `pyramids.dataset.Dataset.read_file`:

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="drought",
    dataset="edo-spaST",
    start="2025-12-21", end="2025-12-21",
    variables=[],
    lat_lim=[40.0, 50.0], lon_lim=[5.0, 15.0],
    path="edo_out",
).download()
# [Path('edo_out/edo-spaST_20251221.tif')]
```

Each indicator only carries data for a limited date range; a request
outside that range surfaces the Copernicus message verbatim:

```python
>>> EarthLens(  # doctest: +SKIP
...     data_source="drought", dataset="edo-cdiad",
...     start="2035-06-21", end="2035-06-21",
...     variables=[], lat_lim=[40.0, 50.0], lon_lim=[5.0, 15.0], path="out",
... ).download()
ValueError: Copernicus EDO/GDO rejected 'edo-cdiad' (HTTP 422): Requested date ... is outside the available coverage range ...
```

The list of curated EDO/GDO ids lives in `src/earthlens/drought/catalog/edo.yaml`
and `gdo.yaml`. The indicator codes + the `TIME` / `SELECTED_TIMESCALE`
param shape were verified live in the A1 gate. Every
`gdo-*` row uses the same `map=DO_WCS` endpoint as the EDO rows — there is
no separate `GDO_WCS` map.

## Aliases

Four facade keys point at the drought backend, all of which require an
explicit `dataset=` kwarg:

* `"drought"` — the canonical key.
* `"usdm"` — discoverability alias; still requires `dataset="usdm"`.
* `"edo"` / `"gdo"` — namespace aliases for the European / Global
  indicator families; the caller names the specific indicator via
  `dataset="edo-spaST"` / `dataset="gdo-twsan"`.

## Date snapping at a glance

```python
from datetime import date
from earthlens.drought._helpers import snap_to_cadence

# Historical Tuesday (today is well past the release Thursday) → itself
snap_to_cadence([date(2026, 6, 23)], "weekly",
                today=date(2027, 1, 1))   # [date(2026, 6, 23)]
# Same Tuesday queried on the same Tuesday (release Thursday is future) → walks back
snap_to_cadence([date(2026, 6, 23)], "weekly",
                today=date(2026, 6, 23))  # [date(2026, 6, 16)]
snap_to_cadence([date(2026, 6, 15)], "10day")    # [date(2026, 6, 11)] — middle dekad
snap_to_cadence([date(2026, 6, 25)], "monthly")  # [date(2026, 6, 1)]
```

A range of dates collapses to one snapped period per release — so a
week-long USDM range yields one FeatureCollection per Tuesday valid date.
The weekly snap walks back one extra week when the same-week Tuesday's
composite has not yet been released (its release Thursday is still in
the future relative to `today`); historical queries always land on the
requested Tuesday.

## Attributions logged on success

Every successful `download()` logs the per-source attribution as a
single info line (no `LicenseWarning`):

* **USDM**: "U.S. Drought Monitor — public-domain weekly composite
  produced by NDMC / UNL / USDA / NOAA. Cite the National Drought
  Mitigation Center."
* **EDO/GDO**: "Copernicus European/Global Drought Observatory (EMS) —
  free reuse with attribution to Copernicus EMS."
* **SPEIbase**: "CSIC Standardised Precipitation-Evapotranspiration
  Index database v2.11 (Vicente-Serrano et al.), CC-BY 4.0."
