# Datasets by provider

Which datasets each earthlens backend serves. Use it to go from a dataset you want to the `data_source` key that
delivers it, and from a provider to what it offers. Grouped by thematic distribution.

**How to read the counts.** Many backends ship a **curated** set of ready-to-use keys over a much larger
**addressable** universe on the underlying server/protocol — shown as `N curated / M addressable`. A plain `N` means
the catalog is the whole universe. Some backends are **query services** (you pass a taxon, country, parameter, sensor
or station — not a dataset id), noted as such.

- The **Example keys** are concrete values you pass as `variables=`, `dataset=`, or the request key — verbatim from
  the catalog.
- To find a dataset from the CLI: `earthlens datasets where <name>` (which provider exposes it),
  `earthlens datasets search <query>` (free-text + `--filter facet=value`), or `earthlens datasets list -p <provider>`
  (enumerate one backend). Add `--include-available` to reach past the curated keys into a backend's full upstream
  index. See the [command-line interface](cli.md) page for the full command set.
- Which **extra** (if any) each backend needs is on the companion [Provider extras](provider-extras.md) page.

## Atmosphere (`earthlens-atmosphere`)

| Provider | What it serves | Example keys | Datasets |
|---|---|---|---|
| `chc` | Climate Hazards Center rainfall & temperature — CHIRPS-2.0, CHIRPS v3, CHIRP, CHIRTS (daily+monthly), CHIRPS-GEFS, SPI/SPEI, CHC_CMIP6 deltas, CHPclim/WBGT | `global-daily`, `africa-daily`, `chirts-daily-tmax`, `wbgt-monthly` | ~97 curated |
| `s3` | AWS Open Data buckets — ERA5, Sentinel-2 L2A, USGS Landsat, NAIP, GOES, Copernicus DEM, ESA WorldCover | `era5`, `sentinel-2-l2a`, `copernicus-dem`, `esa-worldcover` | 7 |
| `ecmwf` | Copernicus Data Stores (CDS + ADS + EWDS) — ERA5/ERA5-Land, CARRA, CERRA, CMIP5, CORDEX, EFAS, GloFAS, CEMS-Fire, CAMS, C3S satellite CDRs, seasonal | `reanalysis-era5-single-levels`, `reanalysis-era5-land`, `cems-glofas-forecast`, `satellite-soil-moisture` | ~165 curated |
| `nwp` | Open numerical-weather-prediction forecasts — GFS/HRRR/GEFS (NOAA), IFS/AIFS (ECMWF Open Data), ICON (DWD), GDPS (ECCC), ARPEGE (Météo-France) | `gfs`, `hrrr`, `ifs-hres`, `ens` | 32 models |
| `goes` | NOAA GOES-R ABI products — L1b radiances, L2 cloud/moisture imagery, aerosol, fire, SST/LST | `abi-l2-mcmip`, `abi-l2-cmip`, `abi-l1b-rad`, `abi-l2-aod` | 17 curated |
| `radar` | NOAA NEXRAD WSR-88D Level II radar volumes, keyed by station | `KTLX`, `KFDR`, `KINX`, `KVNX` | 210 stations |
| `cmip6` | CMIP6 climate projections on the Pangeo cloud, addressed by facet tuple (variable × experiment × model × table) | `tas`, `ssp245`, `CanESM5`, `Amon` | facet vocabulary (~515k store rows) |
| `drought` | Drought indicators — USDM, Copernicus EDO + GDO, CSIC SPEIbase | `usdm`, `edo-spaST`, `gdo-smand`, `speibase-12` | 39 |
| `solar_wind_atlas` | Global Solar Atlas + Global Wind Atlas v3 climatology layers | `ghi`, `dni`, `pvout`, `wind_100m` | 16 layers |
| `nrel` | NREL point resource archives — NSRDB (solar), WIND Toolkit (wind) | `nsrdb-psm3`, `nsrdb-tmy`, `wtk` | 3 |
| `pvgis` | PVGIS solar-radiation / PV time-series tools | `seriescalc`, `tmy` | 2 |
| `climate_indices` | NOAA PSL / KNMI teleconnection indices | `oni`, `nao`, `soi`, `amo` | 10 |
| `tropycal` | Tropical-cyclone best tracks by ocean basin (HURDAT2 / IBTrACS) | `north_atlantic`, `east_pacific`, `west_pacific`, `all` | 10 basins |
| `openaq` | OpenAQ v3 measured-parameter query — criteria pollutants, particulates, black carbon, GHGs, met | `pm25`, `no2`, `o3`, `bc` | 30 parameters *(query service)* |
| `airnow` | AirNow US/Canada EPA pollutant observations | `o3`, `pm25`, `pm10`, `no2` | 6 pollutants *(query service)* |
| `eea_aq` | EEA Europe air-quality observations (via `airbase`) | `pm25`, `pm10`, `o3`, `no2` | 6 pollutants *(query service)* |
| `sensor_community` | Sensor.Community citizen-sensor observations | `pm25`, `pm10`, `pm1`, `temperature` | 6 variables *(query service)* |

## Ocean (`earthlens-ocean`)

| Provider | What it serves | Example keys | Datasets |
|---|---|---|---|
| `cmems` | Copernicus Marine (multi-server) — physics, biogeochemistry, SST, sea level, waves, wind, sea ice across marine domains | `cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m`, `ESACCI-GLO-SST-L4-REP-OBS-SST`, `arctic_omi_si_extent` | ~665 curated / 1,251 addressable |
| `erddap` | Generic ERDDAP client (multi-server) — NOAA CoastWatch/NCEI + Coral Reef Watch curated; any dataset on a curated server | `erdMH1chla8day`, `erdPH53sstd8day`, `NOAA_DHW` | 4 curated / 3,061 addressable |
| `nwm` | NOAA National Water Model — products × operational configurations | products `chrtout`/`ldasout`/`forcing`; configs `short_range`/`medium_range`/`analysis_assim` | 6 products × ~70 configs |
| `caravan` | Large-sample hydrology extensions — daily streamflow + ERA5-Land forcing + static attributes | `base`, `grdc`, `germany`, `spain` | 7 extensions |
| `usgs_water` | USGS NWIS water data — parameter-coded (daily, instantaneous, samples, peaks, ratings, …) | `discharge` (00060), `gage_height` (00065), `temperature` (00010) | 12 curated / ~19,675 codes |
| `argo` | Argo float ocean profiles — physical (`phy`) + biogeochemical (`bgc`) parameters | `TEMP`, `PSAL`, `DOXY`, `CHLA` | 2 families / 15 params *(query service)* |
| `obis` | OBIS marine species occurrences, keyed by scientific name | `common-dolphin`, `blue-whale`, `great-white-shark` (or `species:Mola mola`) | *(query service)* |

## Imagery (`earthlens-imagery`)

| Provider | What it serves | Example keys | Datasets |
|---|---|---|---|
| `gee` | Google Earth Engine — the full EE catalog, auto-grouped (optical, land-cover, hydrology, elevation, climate, atmosphere, precip, SAR) | `COPERNICUS/S2_SR_HARMONIZED`, `LANDSAT/LC08/C02/T1_L2`, `ECMWF/ERA5_LAND/HOURLY` | 1,104 |
| `stac` | STAC across 9 endpoints — Planetary Computer, CDSE, Earth Search, DE Africa/Australia, BDC, VEDA, USGS Landsat, EODC (Copernicus GFM + EODC raster collections) | `sentinel-2-l2a`, `landsat-c2-l2`, `cop-dem-glo-30`, `eodc/gfm` | ~304 curated / ~1,130 addressable |
| `earthdata` | NASA Earthdata — flagship EOSDIS collections across 9 DAACs (GPM, MODIS/VIIRS, GEDI, ICESat-2, ECOSTRESS, EMIT, HLS, SMAP, MUR SST, TEMPO, PACE) | `GPM_3IMERGHHL_07`, `MOD13Q1_061`, `ATL06_006`, `HLSS30_20` | 46 curated / ~8,029 addressable |
| `eumetsat` | EUMETSAT Data Store — MTG/MSG/MFG, Metop(-SG), Sentinel-3/-5P/-6, OSI SAF | `msg-hrseviri`, `mtg-fci-l1c`, `sentinel5p-l2-no2` | 180 |
| `jaxa` | JAXA archive — jaxa-earth (STAC/COG), G-Portal (mission products), P-Tree (Himawari) | `aw3d30`, `gsmap`, `sgli-chla-d-daily` | 918 |
| `asf` | Alaska Satellite Facility SAR — Sentinel-1 SLC/burst/GRD, ALOS PALSAR, OPERA, NISAR, ARIA GUNW, legacy | `sentinel-1-slc`, `sentinel-1-burst`, `alos-palsar-slc`, `opera-rtc-s1` | 42 |
| `sentinel_hub` | Sentinel Hub server-side render (CDSE-free) — S2/S1/S3/S5P/Landsat/DEM + evalscript recipes | `sentinel-2-l2a`, `sentinel-1-iw`, recipe `sentinel-2-l2a-ndvi` | 9 collections + 26 recipes |
| `openeo` | openEO (CDSE) collections + server-side recipes — S2/S1/S3/S5P, Landsat mosaic, WorldCover | `sentinel-2-l2a`, `sentinel-1-grd`, recipe `sentinel-2-l2a-ndvi-monthly` | 19 collections + 5 recipes |

## Land (`earthlens-land`)

| Provider | What it serves | Example keys | Datasets |
|---|---|---|---|
| `ghsl` | JRC Global Human Settlement — population, built-up surface/volume/height/class, settlement model (SMOD), land, WUP projections | `GHS_POP`, `GHS_BUILT_S`, `GHS_BUILT_H_ANBH`, `GHS_SMOD` | 29 |
| `worldpop` | WorldPop — population counts, age/sex, density, births/pregnancies, projections, + 54 covariate layers | `pop`, `age_structures`, `pop_density`, `cviirs` | 11 families + 54 covariates |
| `soilgrids` | ISRIC SoilGrids 2.0 soil properties (250 m, WCS) × 6 depths × 5 quantiles | `clay`, `phh2o`, `soc`, `bdod` | 11 properties |
| `dem` | Copernicus DEM global land elevation — COG tiles on AWS | `cop-dem-glo-30`, `cop-dem-glo-90` | 2 |
| `bathymetry` | Global topography/bathymetry DEMs (NOAA ERDDAP GEBCO/ETOPO1) + EMODnet European high-res DTM (OGC WCS) | `gebco_2020`, `etopo1_ice`, `etopo1_bedrock`, `emodnet` (+ `_2016`/`_2018`/`_2020`/`_2022`) | 8 |
| `glaciers` | Glacier outlines + mass balance — RGI 7.0, GLIMS, WGMS FoG | `rgi:outlines`, `glims:outlines`, `wgms:mass_balance` | 5 |
| `gbif` | GBIF species occurrences by taxon + bbox + time | `animals`, `plants`, `birds`, `mammals` (or `taxon:<name>`) | *(query service)* |
| `wdpa` | Protected Planet (WDPA) protected-area polygons by country | `KEN`, `BRA`, `IDN` (ISO3, or a WDPA id) | *(query service)* |
| `iucn` | IUCN Red List assessments by country or species | `KE`, `BR`, `ID` (ISO2, or `species:<binomial>`) | *(query service)* |

## Hazards (`earthlens-hazards`)

| Provider | What it serves | Example keys | Datasets |
|---|---|---|---|
| `emdat` | EM-DAT disaster impacts — Dataverse events table + GDIS geocoded points/polygons | `emdat:events`, `gdis:points`, `gdis:polygons` | 3 |
| `gdacs` | Live GDACS disaster alerts by hazard type | `EQ`, `TC`, `FL`, `WF` | 6 hazard types *(query service)* |
| `fdsn` | Earthquake events across FDSN networks (USGS / EMSC / INGV / …) | `USGS`, `EMSC`, `INGV`, `EARTHSCOPE` | 6 networks *(query service)* |
| `firms` | NASA FIRMS active-fire detections by sensor | `VIIRS_SNPP_NRT`, `MODIS_SP`, `VIIRS_NOAA20_NRT`, `LANDSAT_NRT` | 9 sensors *(query service)* |
| `osm` | OpenStreetMap features — Overpass (current-state), ohsome (history), PBF extracts | `overpass:hospitals`, `ohsome:buildings`, `pbf:roads` | 14 queries + 9 regions |
| `overture` | Overture Maps themes | `buildings`, `places`, `transportation`, `divisions` | 6 themes / 15 types |
| `admin` | Administrative boundaries — geoBoundaries, CGAZ, Natural Earth, TIGER | `geoboundaries:adm1`, `cgaz:adm0`, `natural_earth:countries`, `tiger:county` | 15 |
| `hdx` | HDX / CKAN humanitarian data — population, boundaries, HOTOSM, WFP, UNHCR, ACLED, + any CKAN id | `kontur-population`, `wfp-food-prices`, `acled-political-violence` | 49 curated / 41,289 addressable |
| `risk_indicators` | Country risk indices — ThinkHazard!, INFORM Risk, Global Forest Watch | `thinkhazard:flood_river`, `inform:risk`, `gfw:tree_cover_loss` | 20 |

## The same dataset, from more than one provider

Some datasets are reachable through several backends — pick the one whose access model suits you (direct file
download vs. server-side processing vs. cloud-native tiles):

| Dataset | Providers |
|---|---|
| **ERA5 / ERA5-Land** reanalysis | `ecmwf` (CDS request), `s3` (AWS `era5-pds`), `gee`, `nwp` (forecast cousin) |
| **Sentinel-2 L2A** | `stac`, `gee`, `openeo`, `sentinel_hub`, `s3` |
| **Sentinel-1** SAR | `asf` (SLC/burst), `stac`, `openeo`, `sentinel_hub`, `gee` (GRD) |
| **CHIRPS** precipitation | `chc` (source, FTP), `gee` |
| **Copernicus DEM** (GLO-30/90) | `dem`, `s3`, `stac`, `gee` |
| **ESA WorldCover** | `s3`, `gee`, `openeo` |
| **GOES-R ABI** | `goes`, `s3` |
| **GloFAS / EFAS** river discharge | `ecmwf` (EWDS) |

The count column reflects a snapshot of the catalogs; broad platforms (`gee`, `stac`, `earthdata`, `cmems`,
`erddap`, `hdx`) keep growing, and their addressable universe is always larger than the curated keys shown here.
