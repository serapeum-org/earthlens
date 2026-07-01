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
| `weekly` | USDM | Most recent **Tuesday** at or before the requested date, **then walk back one more week if that Tuesday's composite has not yet been released** (its release Thursday is still in the future relative to `today`). USDM releases on Thursday for the prior Tuesday's valid date, and the JSON URL is keyed on the Tuesday valid date. |
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

## How the EDO/GDO route works

The Copernicus EMS drought endpoint is **not a conformant OGC WCS server** —
it is a REST shim. Only its `GetCoverage` operation is reliable; the
standard `GetCapabilities` / `DescribeCoverage` discovery operations answer
`502` / `400` (they require a non-standard `SELECTED_TIMESCALE` parameter),
so pyramids' `Dataset.from_wcs` (which wraps GDAL's WCS driver and needs
that discovery handshake) cannot read it.

Instead the backend builds the documented `GetCoverage` URL by hand with
core `requests` and two Copernicus-custom parameters:

* `TIME=<YYYY-MM-DD>` — the snapped period (10-day dekad or month).
* `SELECTED_TIMESCALE=<NN>` — the SPI / SPEI integration window in months
  (`01`, `03`, `06`, `12`, …). The SPI coverages require it; the other
  indicators accept and ignore it. Each `edo-*` / `gdo-*` catalog row
  carries a `timescale` (default `"01"`, a config-as-code tunable).

plus a `SUBSET=Long(west,east)` / `SUBSET=Lat(south,north)` bbox and
`CRS=EPSG:4326&format=GEOTIFF`. The response is streamed to disk and opened
through `pyramids.dataset.Dataset.read_file` to validate it is a real
raster — no `owslib`, no GDAL WCS driver, no `xarray`. When the server
rejects a request (e.g. a date outside an indicator's available coverage
range, an HTTP 422), the Copernicus error message is surfaced verbatim.

**One map, not two.** The Copernicus WCS exposes a single `map=DO_WCS`;
there is no separate `GDO_WCS` map. The European-vs-global split is a
data-extent distinction, so every `gdo-*` row uses the same `map=DO_WCS`
endpoint as the EDO rows (the two Low-Flow Index codes
`lfinx_300_sms` / `lfinx_300_mds` are genuinely GDO-specific).

All three transports are fully wired against the shipped pyramids reader
stack — no extra pyramids work is needed.
