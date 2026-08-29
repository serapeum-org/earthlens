# Change Log

## 0.19.0 (2026-08-29)

### Feat

- **ecmwf**: hydrate every placeholder by probing each variable in turn (#1113)

### Fix

- **ecmwf**: keep aggregated outputs unique and faithful to the request (#1112)

## 0.18.0 (2026-08-23)

### BREAKING CHANGE

- the four inform:* Risk datasets change what they
return. They read the published release workbook instead of the Scores
API, so scores come from the current release (Kenya 6.2, against 5.8
from the workflow the API still serves), the frame gains a source
column, validity_year carries a real year instead of 0, and workflow_id
is empty on a workbook row. No signatures change - source="api"
restores the API behaviour.

### Feat

- **risk_indicators**: serve INFORM's currently published release (#1102)
- **gee**: read raw assets through the pyramids-eo EEDAI fast-path (#1092)
- **fdsn**: add optional ShakeMap raster side-output for USGS events (#1094)

### Fix

- **eea_aq**: mask EEA no-data sentinels to NaN (#1107)
- **ecmwf**: require evidence before the hydrator pairs a lone slug (#1105)
- **ecmwf**: broaden pre-aggregated detection to flux monthly/daily families (#1101)
- **aggregate**: stop op="auto" over-counting pre-aggregated CDS datasets (#1095)

### Refactor

- **base**: consolidate the typed availability errors and status extractors (#1106)

## 0.17.0 (2026-08-22)

### Feat

- **ecmwf**: add the ECDS and XDS data stores (#1055)
- **gee**: add cloud_mask and filters hooks to the GEE backend (#1090)

### Fix

- **admin**: re-enable geoBoundaries ADM1 e2e via pyramids-gis 0.54.0 (#1081)

### Refactor

- **auth**: resolve single-secret credentials via a shared base (#1084)

## 0.16.0 (2026-08-20)

### BREAKING CHANGE

- an omitted path= now writes to the configured output
directory instead of the working directory, and the facade uses
<output_dir>/<source> rather than ./earthlens-data/<source>. path=""
still means the working directory. Old caches are not migrated.

### Feat

- **core**: configurable cache dir, EUMETSAT end-bound fix, and the 2026 eclipse showcase (#1070)
- **eea_aq**: add adjacent-era fallback and split empty-result signals (#1066)
- **ecmwf**: curate the GloFAS historical intermediate stream (#1039)

### Fix

- **docs**: repair the brand guide and restore the head overrides
- **osm**: surface opaque ohsome failures as typed, logged errors (#1072)
- **gdacs**: retry SEARCH and skip live tests on a persistent upstream failure (#1071)
- **overture**: resolve the release live instead of globbing the bundled pin (#1069)
- **earthdata**: force IPv4 on the shared Earthdata Login path only on a dead IPv6 route (#1035)

### Refactor

- **base**: consolidate the AOI-sidecar cache and windowed /vsicurl read (#1067)

## 0.15.0 (2026-08-14)

### Feat

- **e2e**: add a per-provider live-e2e-on-change gate and skip lanes on upstream outages (#1017)
- **stac**: add Copernicus GFM and the EODC STAC endpoint (#1015)
- **bathymetry**: add EMODnet Bathymetry European DTM over OGC WCS (#1011)
- add GESLA, FLOPROS, and CatRaRE flood-data backends (batch 2) (#1000)
- **isimip**: add the earthlens.isimip bias-adjusted climate-forcing backend (#999)
- **flodis**: add the FLODIS observed flood footprints–impacts backend (#998)
- **radklim**: add the DWD RADKLIM / RADOLAN radar-precipitation backend (#981)
- **fabdem,jrc-flood**: add FABDEM bare-earth DEM and JRC European Flood Hazard Map backends (#958)
- **aqueduct**: add the WRI Aqueduct riverine flood-risk backend  (#957)
- **nsi**: add earthlens.nsi US flood exposure & loss backend (NSI / NFHL / NFIP) (#949)
- **hanze**: add historical European flood-impacts backend (#948)

### Fix

- **osm**: handle ohsome 403/429 throttling with a typed error and retry (#1027)
- **ghsl**: fail a dead JRC host fast with a split connect/read timeout (#1016)
- **stac**: pin anonymous S3 reads to the region endpoint where required (#937)

### Refactor

- **cli**: make core's CLI backend-agnostic via entry-point discovery (#1021)

## 0.14.0 (2026-08-07)

### Feat

- **mswep**: add the GloH2O MSWEP / MSWX backend (#889)

## 0.13.0 (2026-08-02)

### BREAKING CHANGE

- a backend subclassing another backend now raises
TypeError unless it declares ergonomics_resolved=True, and _create_grid
/ _check_input_dates must return SpatialExtent / TemporalExtent rather
than a dict or None. Both affect downstream subclasses only; all 48
in-repo backends are unchanged, and download(aggregate=None) still
works.

### Feat

- **caravan**: add the Caravan large-sample hydrology backend (open GRDC route) (#887)
- **ecmwf**: three-store Copernicus Data Store backend (CDS + ADS + EWDS) (#871)
- **emdat**: add the EM-DAT disaster-impacts backend (#888)

### Refactor

- **base**: enforce the shared contracts instead of restating them (#853)

## 0.12.0 (2026-07-27)

### BREAKING CHANGE

- Catalog.load(<missing path>) raises ValueError, not
FileNotFoundError, now that all 48 catalogs report a missing file through
the shared loader. OpenAQ(limit=...) was a page size and is now
page_size=; limit= is a total row cap, so code passing limit=1000 for
paging must pass page_size=1000. earthlens.jaxa.catalog.Catalog.load()
returns a fresh instance per call rather than a shared one.
- an unsupported temporal_resolution now raises instead
of silently downloading daily; cadence="weekly" maps to 7D rather than
pandas W (which emits period ends and skips the window's first days);
aggregate._reduce and _window_groups are now reduce_time_axis and
window_groups.

### Fix

- **base**: bound memory, pool connections and unify the duplicated base contracts (#824)
- **base**: repair four data-source contract bugs and remove the duplication behind them (#804)

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
