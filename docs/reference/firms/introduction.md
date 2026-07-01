# FIRMS — introduction

[NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) (Fire Information for
Resource Management System) distributes **active-fire / thermal-anomaly
detections** from the MODIS (Terra/Aqua, C6.1), VIIRS
(Suomi-NPP / NOAA-20 / NOAA-21), geostationary GOES (ABI), and Landsat
(8/9) instruments. Each detection is a single
fire pixel — a point with a location, an acquisition time, a brightness
temperature, a detection confidence, and a fire-radiative-power (FRP)
estimate. FIRMS serves both a **near-real-time** stream (detections
within hours of overpass) and a **standard-quality archive** going back
to the start of each mission.

This page orients the `earthlens` FIRMS backend. For the hands-on
download walkthrough see [Usage](usage.md); for credentials see
[Authentication](authentication.md); the rendered API is the
[Reference](firms.md) page.

## What earthlens returns

A FIRMS detection is an inherently **vector** datum — a geolocated point,
not a gridded cell — so the backend returns a pyramids
`FeatureCollection` (a `geopandas.GeoDataFrame`, CRS `EPSG:4326`), one
row per fire pixel:

| Column | Meaning |
|---|---|
| `latitude` / `longitude` | detection centroid (WGS84) |
| `acq_datetime` | acquisition timestamp (tz-aware UTC) |
| `sensor` | the requested FIRMS sensor code |
| `satellite` | reporting platform (`Terra`, `Aqua`, `N`, …) |
| `confidence` | raw confidence **as a string** — `"85"` (MODIS) or `l`/`n`/`h` (VIIRS); use `confidence_pct` for numeric work |
| `confidence_pct` | normalised `0-100` confidence (categorical tokens → 25/60/90; GOES passes its provider value through) |
| `brightness_k` | brightness temperature in kelvin |
| `frp` | fire radiative power (MW) |
| `daynight` | `D` (day) or `N` (night) overpass |
| `geometry` | `Point(longitude, latitude)` |

This makes FIRMS a **`vector`** backend (`FIRMS.OUTPUT_KIND == "vector"`).
Because a detection table is not a gridded array, the `EarthLens` facade
**rejects an `aggregate=` argument** for this backend — the raster
time-window reducer has no meaning on point detections. Post-process the
returned `GeoDataFrame` directly instead (count per day, sum FRP, rasterise
with pyramids, …).

## Why it matters here

The raster backends (ERA5 via CDS, MODIS/VIIRS imagery and the
burned-area products via Google Earth Engine) give you gridded fields
with full spatial coverage. FIRMS is the complementary **event feed**:
sparse but direct per-pixel fire detections, refreshed within hours.
A common workflow pairs FIRMS detections with a GEE burned-area product
(MCD64A1, FireCCI) or a CDS meteorology field over the same bbox and
window to study fire onset, spread, and weather drivers.

## Things to know up front

- **Detections, not perimeters.** FIRMS reports where a sensor saw a
  thermal anomaly at overpass time — not the final fire perimeter or the
  total area burned. (FIRMS' `data_availability` index also lists
  `BA_MODIS` / `BA_VIIRS`, but those are **burned-area raster** products,
  not area-CSV active-fire sources — they return nothing through this
  backend and are deliberately not catalogued. For gridded burned-area
  rasters (MCD64A1, FireCCI) use the Google Earth Engine backend.)
- **A free `MAP_KEY` is required.** Every request carries a `MAP_KEY` as
  a URL path segment. Request a free key at
  <https://firms.modaps.eosdis.nasa.gov/api/map_key/>; see
  [Authentication](authentication.md).
- **NRT vs archive.** `*_NRT` sensors cover only roughly the **last two
  months**; older data lives in the `*_SP` standard-quality archive
  sensors. A request for an old window against an `*_NRT` sensor returns
  an empty result, so the backend logs a warning naming the `*_SP`
  variant — it does not silently swap the sensor.
- **Confidence differs by family.** MODIS reports a numeric `0-100`
  confidence; VIIRS reports a categorical `l`/`n`/`h`; LANDSAT reports
  `l`/`m`/`h`; GOES reports a provider-defined numeric value. The backend
  keeps the raw value and adds a uniform `confidence_pct` (categorical
  tokens map to 25/60/90) so one `min_confidence=` threshold works across
  MODIS / VIIRS / LANDSAT. GOES confidence is *not* a 0-100 percent, so
  `min_confidence` is skipped for GOES (with a warning) rather than
  silently dropping its detections.
- **A 5-day-per-request cap and a transaction quota.** FIRMS serves at
  most 5 days per request and one sensor per request, and allows ~5000
  transactions per rolling 10 minutes. The backend chunks longer windows
  transparently and throttles against the quota with a back-off — both
  are invisible to the caller. See [Usage](usage.md).
