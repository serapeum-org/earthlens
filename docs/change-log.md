# Change Log

## Unreleased

### Feat

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
  docs pages, four example notebooks (catalog explorer + anonymous quickstart
  + InSAR stack walkthrough + OPERA RTC workflow), and an `e2e-asf` weekly-cron
  CI lane.

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
