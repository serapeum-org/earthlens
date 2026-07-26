# Change Log

## Unreleased

### BREAKING CHANGE

- `Catalog.load(<missing path>)` now raises `ValueError` naming the path,
where it previously raised `FileNotFoundError`. Every catalog reports a
missing file the same way now that all 48 share
`earthlens.base.catalog_source.load_catalog` — the last five hold-outs (chc,
earthdata, gee, hdx, jaxa) and radar were migrated too, so no loader
computes its own `st_mtime_ns` cache key any more. No shipped caller catches
`FileNotFoundError` around a catalog load, but external code that does
should catch `ValueError` instead.
- `OpenAQ(limit=...)` was a *page size*, not a total. The page size is now
`page_size=` and `limit=` is what it read like: a cap on the measurement rows
`download()` returns and writes, across every sensor, enforced as the
per-sensor frames arrive so sensors past the cap are never fetched. Code
passing `limit=1000` for paging should pass `page_size=1000`.

- `earthlens.jaxa.catalog.Catalog.load()` returns a fresh instance per call
rather than a shared cached one (the parse payload is what is memoised), so
mutating the result no longer corrupts later loads. `_yaml_files_for` is gone
from the jaxa and gee catalog modules; `_mtime_ns` is gone from hdx's.
- The catalog-import error text changed from
`"Backend 'gee' catalog is unavailable"` to
`"Backend catalog for 'gee' is unavailable"`, since the registry and
catalog paths now share one message builder.
- An `ImportError` raised *inside* a backend is no longer rewritten into
`"its runtime dependency is not installed. Install with pip install
earthlens[...]"`. Only a genuinely absent third-party SDK gets that hint;
a fault in earthlens' own code propagates unchanged so the real cause is
visible.

- `AbstractCatalog.catalog` is a read-only property rather than a pydantic
field, so it no longer appears in `model_fields` or `model_dump()`, and
`Catalog(catalog=...)` is ignored instead of setting it. Reads
(`cat.catalog["key"]`, `in`, `len`, iteration) are unchanged; writes now
raise instead of silently rewriting `datasets`.

### Feat

- A bounded-result contract on `AbstractDataSource`, so a large vector or
tabular request no longer has to be held whole in memory:
  - `EarthLens.iter_download(limit=...)` (and
    `AbstractDataSource.iter_download`) streams one fragment per product, so a
    caller can write or reduce each one and let it go. Backends with the
    `_search` / `_fetch_one` split get it for free; a backend that answers the
    whole request in one call raises `NotImplementedError` rather than
    pretending to stream.
  - `limit=` is a cap on **total** rows / features, not a page size. It is
    exact when it lands mid-fragment, and it stops the work: products past the
    cap are never fetched. `check_limit` rejects `0`, negatives and non-ints
    instead of quietly returning nothing.
  - `_take_limited` is the shared helper backends use to apply that cap while
    their per-item fragments arrive.
- Every backend whose batch is a loop over independent items now accepts
`errors="warn" | "raise" | "ignore"`: `chc`, `cmems`, `ecmwf` and `radar` join
`fdsn`, `nwp` and `soilgrids`. `_search_fetch_each` also takes `errors=`.

### Fix

- `Catalog.load()` hands out a fresh instance per call. Previously one
caller mutating `.datasets` corrupted the catalog for the whole process,
including a later `Catalog()`. The row objects inside are still shared
frozen models and should be treated as read-only.
- A `download()` the backend rejects no longer leaves an empty output
directory behind.
- DWD NWP downloads reject a truncated `.bz2` body instead of publishing a
short `.grib2`, and decode multi-stream `.bz2` fully.

## 0.11.0 (2026-07-20)

### BREAKING CHANGE

- `from earthlens import EarthLens` no longer works;
import from earthlens.core instead (also download, find, search,
sources, AggregationConfig, aggregate_netcdf, __version__).

### Feat

- **packaging**: split providers into a uv workspace of 7 distributions (earthlens.core namespace) (#784)
- **osm**: add a third pbf protocol for bulk Geofabrik extracts (#727)

### Fix

- **release**: keep changelog_file within the release action's grep window

### Refactor

- **base**: consolidate duplicated GIS logic and adopt pyramids 0.46.0 (#728)

## 0.10.0 (2026-07-07)

### Feat

- **dem**: add anonymous Copernicus DEM backend (M3) (#699)
- **cmip6**: add backend for the raw CMIP6 archive on gs://cmip6 (#700)
- **goes**: add earthlens.goes NOAA GOES-R ABI backend (#679)
- **jaxa**: add P-Tree Himawari HSD protocol branch (third JAXA protocol) (#677)
- **air-quality**: add airnow, eea_aq & sensor_community ground-obs backends (#676)
- add aifs-ens NWP row and optional ecmwf-modern client (#678)
- **base**: add HttpClient and region_affinity, migrate 10 backends onto them (#667)
- **nwp**: add mode={subset,whole} download override and catalog title/description (#655)
- **ecmwf**: add EWDS endpoint with per-endpoint CADS routing and GloFAS forecast (#656)
- **soilgrids**: add earthlens.soilgrids backend (ISRIC SoilGrids 2.0 via OGC WCS) (#633)
- **eumetsat**: add Data Tailor server-side customisation (tailor=) (#650)
- **drought**: add earthlens.drought backend (USDM, EDO/GDO, SPEIbase) (#514)
- **glaciers**: add earthlens.glaciers backend (RGI 7.0 + GLIMS + WGMS) (#612)
- **osm**: add earthlens.osm backend (OpenStreetMap via Overpass + ohsome) (#622)
- **admin**: add earthlens.admin backend (geoBoundaries / CGAZ / Natural Earth / TIGER) (#593)
- **nrel**: add earthlens.nrel backend (NREL NSRDB + WIND Toolkit time series) (#563)
- **solar-wind-atlas**: add earthlens.solar_wind_atlas backend (M26) (#562)
- **risk-indicators**: add earthlens.risk_indicators backend (ThinkHazard! + INFORM + GFW) (#583)
- **pvgis**: add earthlens.pvgis backend (JRC PVGIS 5.3 solar / PV time series) (#543)
- **climate-indices**: add earthlens.climate_indices backend (NOAA PSL + KNMI Climate Explorer) (#532)
- **argo**: add earthlens.argo backend for Argo float ocean profiles (#504)
- **erddap**: add generic ERDDAP backend (griddap raster / tabledap tabular) (#490)
- **bathymetry**: add earthlens.bathymetry DEM backend (GEBCO 2020 + ETOPO1) (#503)
- add the biodiversity backend cluster (gbif, obis, wdpa, iucn) (#470)
- add the biodiversity backend cluster (gbif, obis, wdpa, iucn)

  Four new provider backends share one request shape (taxon / species /  
  area selector over a bbox) and a small set of helpers:  

  - gbif (anonymous, pygbif) -> vector occurrence FeatureCollection  
  - obis (anonymous, pyobis) -> vector occurrence FeatureCollection  
  - wdpa / protected-planet (WDPA_TOKEN ?token=) -> vector polygons  
  - iucn / redlist (IUCN_TOKEN Bearer) -> tabular DataFrame  

  Shared in earthlens.biodiversity:  

  - wkt_from_bbox: SpatialExtent -> ccw POLYGON((...)) for geometry=  
  - occurrences_to_fc: pygbif list[dict] / pyobis DataFrame ->  
    EPSG:4326 points FeatureCollection (modelled on fdsn.events)  
  - LicenseWarning / warn_license: promoted out of overture/_helpers  
    with re-export so overture's is-identity is preserved (G8)  

  Each backend ships a catalog.py + curated YAML matching the standard  
  subpackage layout, plus a richer available_datasets index (gbif 36  
  taxa, obis 30 marine groups, wdpa/iucn 40 country codes each). The  
  cluster is wired into every earthlens datasets CLI surface (list,  
  show, search, where, validate, refresh, audit, curate, stanza).  

  CI lanes e2e-iucn / e2e-wdpa added in .github/workflows/tests-e2e.yml  
  against secrets.IUCN_TOKEN / secrets.WDPA_TOKEN; gbif/obis run live  
  in nbval-lax. Cluster coverage: 100% line+branch (920/226).  

  New optional extras: gbif (pygbif >=0.6.6), obis (pyobis >=1.6.1);  
  both folded into [all]. wdpa/iucn use core requests.  

  Closes #491, #492, #493, #494, #495, #496, #497, #498, #499, #500, #501, #502
- **jaxa**: add earthlens.jaxa backend (jaxa-earth STAC/COG + G-Portal SFTP) (#469)
- **asf**: new earthlens.asf — Alaska Satellite Facility SAR + InSAR baseline stack (#451)

### Fix

- **cli**: bump distinct-backend count to 31 after erddap merge (#533)

### Refactor

- **http**: migrate 8 more backends onto HttpClient (#713)

## 0.9.0 (2026-06-17)

### Feat

- **stac**: add 5 new STAC endpoints (DEAfrica, DEA, VEDA, USGS LandsatLook, Brazil Data Cube) (#425)
- **nwp**: harden the shipped nwp/nwm backends  (#426)

## 0.8.0 (2026-06-16)

### Feat

- **facade**: improve EarthLens public-API ergonomics (#407)
- **nwm**: add earthlens.nwm (NOAA National Water Model) backend (#225)
- **grids,stac**: adopt the specialized grid adapters and EO-agency signers from pyramids (#386)
- **cli**: add the earthlens catalog-query CLI and retire tools/ (#383)

### Fix

- **cli**: keep datasets --write paths off the Rich wrap boundary (#408)

## 0.7.0 (2026-06-03)

### Feat

- **s3**: rework earthlens.s3 into a registry-driven multi-dataset AWS Open-Data backend (#308)
- **hdx**: add Humanitarian Data Exchange backend (#216)
- **ghsl**: add JRC Global Human Settlement Layer backend (earthlens.ghsl) (#295)
- **worldpop**: add WorldPop population data hub backend (earthlens.worldpop) (#306)
- **catalog**: align catalog design across all backends with the AbstractCatalog contract (#294)
- **sentinel-hub**: add Sentinel Hub server-side-render backend (#259)
- **usgs-water**: add USGS NWIS / Water Data backend (earthlens.usgs_water) (#260)
- **overture**: add Overture Maps vector backend (earthlens.overture) (#247)
- **nwp**: add open NWP forecast backend + NEXRAD radar (NOAA/ECMWF/DWD/MF) (#194)
- **openeo**: add openEO server-side-processing backend (CDSE) (#205)
- **eumetsat**: add EUMETSAT Data Store backend (earthlens.eumetsat) (#204)
- **firms**: add NASA FIRMS active-fire backend (#192)

### Fix

- **tropycal**: shim pkg_resources so tropycal imports under setuptools>=81 (#307)
- **ci**: add py7zr to the [all] extra so the wheel suite installs it (#338)

### Refactor

- **provider**: align backend API surface and subpackage layout (#372)
- **cmems**: decode the CF time axis via pyramids NetCDF, drop xarray (#206)

## 0.6.0 (2026-05-26)

### Feat

- **earthdata**: add NASA Earthdata backend (EOSDIS via earthaccess + CMR) (#148)
- **stac**: unified STAC-API + COG backend (Planetary Computer / CDSE / Earth Search) (#150)
- **tropycal**: add tropical-cyclone backend with best-track, recon, ships and realtime products (#149)
- **openaq**: add OpenAQ v3 air-quality backend (#106) (#106)
- **gdacs**: add GDACS multi-hazard disaster-alert backend (#105)
- **fdsn**: add FDSN seismic-event backend (first vector output) (#72)
- **cmems**: add Copernicus Marine (CMEMS) backend (#73)

## 0.5.0 (2026-05-18)

### Feat

- overhaul GEE backend, add CHC subpackage, extract shared GIS primitives (#53)

## 0.4.0 (2026-05-10)

### Refactor

- rename earthly -> earthlens and fix CDS notebooks (#47)

## 0.3.0 (2026-05-07)

### Feat

- **ecmwf**: migrate from legacy MARS API to cdsapi and rebuild backend, catalog, and tooling (#30)

### Fix

- **pyproject**: update pyramids-gis dependency and add commitizen configuration

## 0.2.2 (2023-01-29)

- Add documentation
- Bump up pyramids versions

## 0.2.1 (2023-01-25)

- Add Amazon S3 data source and catalog for the data available in ERA5 bucket (ERA5 only tested)
- Replace utility functions with the serapeum_utils package

## 0.2.0 (2023-01-15)

- Bump up numpy and pyramids versions
- Create an abstract class for datasource and catalog as a blueprint for all data sources
- Test all classes in CI
- Use pathlib to deal with paths

## 0.1.7 (2022-12-26)

- Fix PyPI package names in the requirements.txt file
- Fix python version in requirements.txt

## 0.1.6 (2022-12-26)

- Use environment.yaml and requirements.txt instead of pyproject.toml and replace poetry env by conda env
- Lock numpy to 1.23.5

## 0.1.5 (2022-12-07)

- First release on PyPI
- Add ECMWF data catalog
