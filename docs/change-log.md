# Change Log

## Unreleased

### Feat

- **soilgrids**: add `earthlens.soilgrids` — ISRIC SoilGrids 2.0 global 250 m
  soil-property maps (clay, sand, silt, cfvo, phh2o, cec, nitrogen, soc, ocd,
  ocs, bdod) subset **server-side over OGC WCS** and written as GeoTIFF
  (`OUTPUT_KIND="raster"`). A request expands `variables` × `depths` ×
  `quantiles` into `(property, depth, quantile)` coverage triples, one GeoTIFF
  per cell named `{property}_{depth}_{quantile}.tif`. The WCS transport is
  consumed from pyramids' `Dataset.from_wcs` (0.38.0) — earthlens imports no
  OGC-WCS SDK; SoilGrids' native Interrupted Goode Homolosine grid
  (`EPSG:152160`, unresolvable in PROJ) is handled with a `coverage_crs` shim and
  reprojected to EPSG:4326 by default. Values are scaled integers (divide by a
  per-property `scale_factor`); the backend records the unit/scale but never
  rescales pixels. A polygon `aoi=` is masked to shape; per-coverage WCS failures
  are isolated (skip-and-continue) under a progress bar; `aggregate=` is rejected
  (static, no time axis). Ships a sharded property catalog, the `soilgrids` /
  `isric` facade keys, a `datasets validate soilgrids` structural lint, a live
  gated e2e, intro / usage / datasets / reference docs, and three example
  notebooks. No extra SDK and no auth module — open, CC-BY 4.0.
- **erddap**: add `earthlens.erddap` — a generic ERDDAP client that reaches many
  public ERDDAP servers (NOAA CoastWatch / Coral Reef Watch, NCEI, …) from one
  backend. A curated sharded catalog pins each dataset to a concrete
  `(server_url, dataset_id, protocol)`; the `protocol` sets a **per-instance**
  `OUTPUT_KIND` — `griddap` → raster NetCDF (accepts `aggregate=`, routed through
  the pyramids `aggregate_netcdf` flow), `tabledap` → tabular `pandas.DataFrame`.
  Built on IOOS `erddapy` (the tabledap `to_pandas()` path only); the griddap
  path builds the OPeNDAP `.nc` URL directly and downloads it (erddapy's instance
  `dataset_id` setter eagerly fetches the full coordinate axis and hangs), reading
  it back via pyramids — earthlens never imports `xarray`. Validates NetCDF magic
  bytes before writing (an ERDDAP HTML error page can arrive with a 200), and
  flux variables are catalog-driven (`flux_variables` → `op="auto"` sums). Ships
  four verified-public CoastWatch datasets (CRW SST anomaly + DHW, NCEI Pathfinder
  SST, Aqua MODIS chlorophyll [historical 2003–2022], NDBC buoys), the `[erddap]`
  optional extra (`erddapy`), aliases `erddap` / `ioos`, intro / usage / datasets
  / reference docs, and three example notebooks (catalog explorer + griddap +
  tabledap). Full `earthlens datasets` CLI integration: `validate erddap` lints
  the rows, `refresh erddap [--write]` regenerates the `available_datasets:`
  index by walking each curated server's `allDatasets` table, `audit erddap`
  diffs curated-vs-live, `audit erddap --coverage` classifies the universe
  into DONE / addressable (griddap) / table (tabledap) / thin (test datasets) /
  missing, and `curate erddap <id> [--server …]` seeds a catalog row from a
  dataset's `/info` metadata. Public servers only (no auth module).
- **asf**: add `earthlens.asf` — Alaska Satellite Facility SAR backend with
  `asf_search`-backed search and the InSAR baseline `stack()`. Reuses NASA
  Earthdata Login auth from `earthlens.earthdata` (no second credential
  system); search runs anonymously, only download authenticates. Ships a
  42-row curated product catalog (Sentinel-1 SLC/BURST/GRD/OCN + per-satellite
  variants, ALOS PALSAR / ALOS-2, the full OPERA-S1 family including
  OPERA-S1-CALVAL, ARIA GUNW, the complete NISAR product family — RSLC /
  GSLC / GCOV / L0B / RIFG / RUNW / GUNW / ROFF / GOFF / LRCLK_UTC — the
  TROPO atmospheric corrections, ERS-1/2, JERS-1, RADARSAT-1, plus SEASAT /
  SIR-C / AIRSAR / UAVSAR / SMAP). Aliases
  `asf` / `alaska-satellite-facility` / `insar`. `aggregate=` rejected with
  `NotImplementedError` — the MVP returns SAR product paths for downstream
  InSAR tooling (HyP3 / ISCE / SNAP / MintPy) rather than processing them
  in-flight. Catalog refresh is hand-maintained; `earthlens datasets validate
  asf` checks every row's PLATFORM / DATASET / PRODUCT_TYPE member against the
  installed asf_search. Adds intro / authentication / usage / available products
  docs pages, five example notebooks (catalog explorer + anonymous quickstart
  + InSAR stack walkthrough + OPERA RTC search workflow + an end-to-end
  backscatter demo that downloads, opens with pyramids, and plots a 6 MB
  OPERA RTC tile in decibels), and an `e2e-asf` weekly-cron CI lane.

### Fix

- **pyproject**: require `pyramids-gis >=0.38.0` (core dependency plus the
  `parquet` / `stac` / `viz` extras) and refresh `pixi.lock`. pyramids 0.38.0
  ships the NetCDF `Container` / `Variable` type split (pyramids #625);
  earthlens already consumes the typed `NetCDF` / `LabeledDataset` / `Dataset`
  entry points and the `variable_names` property, so no source change is
  needed. The lockfile refresh (via the repo-mandated `pixi update`, which
  re-resolves the whole graph) also carries incidental minor/patch bumps of
  unrelated transitives — `geopandas`, `pandas`, `pyogrio`, `typer`,
  `google-auth`, `greenlet`, `httplib2`, `regex` (plus dev/docs `cleopatra`,
  `ipython`) — none crossing a major-version boundary.

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
