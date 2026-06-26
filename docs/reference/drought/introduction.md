# Drought indicators — introduction

The drought backend reaches three public drought-monitoring services through
one unified `EarthLens("drought", dataset=...)` shape:

* **US Drought Monitor (USDM)** — weekly D0–D4 drought-class polygons for the
  continental United States, produced jointly by NDMC, UNL, USDA, and NOAA
  ([droughtmonitor.unl.edu](https://droughtmonitor.unl.edu/)).
* **Copernicus European / Global Drought Observatory (EDO / GDO)** —
  Standardised Precipitation Index, soil-moisture anomaly, fAPAR anomaly,
  Combined Drought Indicator, GRACE TWS anomaly, and friends, served as
  raster GeoTIFFs over **OGC WCS 2.0.0**
  ([drought.emergency.copernicus.eu](https://drought.emergency.copernicus.eu/data/wcs-service)).
* **CSIC SPEIbase** — the global Standardised Precipitation Evapotranspiration
  Index, monthly 0.5° rasters at scales 1–48 months from 1901 to present,
  shipped as NetCDF ([spei.csic.es](https://spei.csic.es/database.html)).

Each catalog row pins one dataset; the backend's `OUTPUT_KIND` is
per-instance — `vector` for USDM (returns a
`pyramids.feature.collection.FeatureCollection` in EPSG:4326) and `raster`
for EDO/GDO and SPEIbase (returns a `list[Path]` of written GeoTIFFs).

## Authentication

None — all three sources are open:

* USDM: public domain weekly composite.
* EDO/GDO: Copernicus EMS free reuse (attribution required).
* SPEIbase: CC-BY 4.0.

On every successful `download()` the backend logs the per-source attribution
line as a single info message — no `LicenseWarning` (none of the three
carry a non-commercial or restricted-redistribution clause).

## Per-dataset cadence and date snapping

Each catalog row carries a `cadence` field; the backend snaps every
requested date onto the source's release calendar before fetching:

| Cadence | Where | Snap rule |
|---------|-------|-----------|
| `weekly` | USDM | Most recent **Tuesday** at or before the requested date (USDM releases Thursday UTC, valid the prior Tuesday — the JSON URL is keyed on the Tuesday valid date, not the Thursday release date). |
| `10day`  | Most EDO/GDO indicators | Start of the **dekad** containing the date (the 1st, 11th, or 21st of the month). |
| `monthly` | SPEIbase + some EDO/GDO indicators (`spgTS`, `twsan`, `rdria`) | The **first** of the month. |

Two dates that snap to the same Tuesday yield one fetch — so a two-week
window over USDM returns at most two `FeatureCollection`s merged into one.

## Output shape

| Dataset family | `OUTPUT_KIND` | `download()` returns |
|---------------|---------------|----------------------|
| `usdm` | `vector` | A `FeatureCollection` of drought-class polygons in EPSG:4326 with `OBJECTID`, `DM` (drought class 0–4), `Shape_Length`, `Shape_Area`, `release_date`. |
| `edo-*` / `gdo-*` | `raster` | One GeoTIFF per snapped period in `path/`. |
| `speibase-*` | `raster` | One GeoTIFF per snapped month in `path/`. |

The facade gates `aggregate=` from the per-instance `OUTPUT_KIND`:
the USDM vector route **rejects** `aggregate=` with `NotImplementedError`
(drought-class polygons have no gridded reduction); the raster routes
forward it (currently behind a `NotImplementedError` placeholder until the
stack reducer ships).

## EDO/GDO status — pending the pyramids temporal WCS reader

The EDO/GDO indicators are served over OGC WCS with a **time-axis subset**
(`subset=time(...)` in WCS 2.0.0). The base `pyramids.wcs.read_wcs` is the
cross-repo `PY-A` task introduced by the soilgrids plan and unblocks the
EDO/GDO half once it ships with the temporal `time=` parameter. Until
then, EDO/GDO catalog rows resolve and validate, the facade routes a
request, and `download()` raises a clear `NotImplementedError` pointing
at the pending dependency.

The USDM (vector) and SPEIbase (NetCDF) routes are fully wired and have
no pyramids dependency beyond what already ships.
