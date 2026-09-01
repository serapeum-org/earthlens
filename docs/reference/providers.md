# Supported data sources

earthlens exposes every provider through one facade, `EarthLens(data_source="<key>", ...)`. The table below lists
the providers that are integrated today, the string key(s) each one accepts, the natural output it produces, and
the optional dependency (extra) you need to install for it.

Install a backend's optional dependency with:

```bash
pip install earthlens[<extra>]      # e.g. earthlens[ecmwf]
pip install earthlens[all]          # every backend's SDK
```

Backends with `—` in the **Extra** column need only the core install — they reach their provider over anonymous FTP or
plain HTTP and pull in no additional SDK.

## Integrated providers

| Provider | `data_source` key(s) | Output | Auth | Extra | Docs |
|---|---|---|---|---|---|
| Climate Hazards Center (CHIRPS / CHIRTS / SPI / SPEI / WBGT / …) | `chc`, `chirps` | raster | anonymous FTP | — | [CHC](chc/introduction.md) |
| AWS Open Data (ERA5 / Sentinel-2 / Copernicus DEM / ESA WorldCover) | `amazon-s3` | mixed | unsigned AWS (public buckets) | `s3` | [Amazon S3](s3/introduction.md) |
| ECMWF Climate Data Store | `ecmwf` | raster | `~/.cdsapirc` token | `ecmwf` | [ECMWF](ecmwf/introduction.md) |
| Google Earth Engine | `gee`, `google-earth-engine` | raster | service account | `gee` | [GEE](gee/introduction.md) |
| Copernicus Marine (CMEMS) | `cmems` | raster | Copernicus Marine login | `cmems` | [CMEMS](cmems/introduction.md) |
| FDSN seismic events (USGS / EMSC / INGV / …) | `fdsn` | vector | none | `fdsn` | [FDSN](fdsn/introduction.md) |
| GDACS disaster alerts | `gdacs` | vector | none | — | [GDACS](gdacs/introduction.md) |
| EM-DAT disaster impacts | `emdat`, `gdis` | tabular | registered account (token) | `emdat` | [EM-DAT](emdat/introduction.md) |
| OpenAQ air quality (global aggregator) | `openaq` | tabular | API key (`X-API-Key`) | — | [OpenAQ](openaq/introduction.md) |
| AirNow air quality (US / Canada EPA) | `airnow` | tabular | API key (`API_KEY`) | — | [AirNow](airnow/introduction.md) |
| EEA air quality (Europe) | `eea-aq` | tabular | none (public) | `eea_aq` | [EEA](eea-aq/introduction.md) |
| Sensor.Community air quality (crowdsourced) | `sensor-community` | tabular | none (public) | — | [Sensor.Community](sensor-community/introduction.md) |
| Tropycal tropical-cyclone tracks | `tropycal` | vector (tabular for SHIPS) | none | `tropycal` | [Tropycal](tropycal/introduction.md) |
| STAC — Planetary Computer / CDSE / Earth Search | `stac`, `cdse` | raster | per-endpoint (anonymous / MPC SAS / CDSE S3) | `stac` | [STAC](stac/introduction.md) |
| NASA Earthdata (9 EOSDIS DAACs via `earthaccess`) | `earthdata` | per-dataset (raster / vector / tabular) | EDL login or bearer token | `earthdata` | [Earthdata](earthdata/introduction.md) |
| Open NWP forecasts (NOAA NODD / ECMWF Open Data / DWD via Herbie) | `nwp` | raster | none (open buckets) | `nwp` | [NWP](nwp/introduction.md) |
| NEXRAD Level-II radar (real-time chunk feed) | `radar`, `nexrad` | vector | none (anonymous S3) | `radar` | [NEXRAD radar](radar/introduction.md) |
| openEO server-side processing (defaults to CDSE) | `openeo` | raster | CDSE OIDC (interactive or client-credentials) | `openeo` | [openEO](openeo/introduction.md) |
| GloH2O MSWEP / MSWX (approved Drive share) | `mswep`, `mswx`, `gloh2o` | raster (NetCDF granules) | any Drive credential (service account / ADC / OAuth / rclone) - link-shared, access granted per person | `mswep` | [MSWEP](mswep/introduction.md) |
| NOAA National Water Model (`noaa-nwm-pds`) | `nwm`, `national-water-model` | per-product (`chrtout` tabular / `ldasout` raster) | unsigned AWS (public bucket) | `nwm` | [NWM](nwm/introduction.md) |
| Humanitarian Data Exchange (UN OCHA, CKAN) | `hdx` | mixed | none (public) | `hdx` | [HDX](hdx/introduction.md) |
| NASA FIRMS active fire detections | `firms` | vector | `FIRMS_MAP_KEY` | — | [FIRMS](firms/introduction.md) |
| EUMETSAT Data Store | `eumetsat` | per-dataset (raster default) | consumer key / secret | `eumetsat` | [EUMETSAT](eumetsat/introduction.md) |
| Sentinel Hub server-side render (CDSE) | `sentinel-hub`, `sentinelhub` | mixed (raster / tabular per plane) | OAuth client id / secret | `sentinel-hub` | [Sentinel Hub](sentinel-hub/introduction.md) |
| Overture Maps vector basemap | `overture` | vector | none (public) | `overture` | [Overture](overture/introduction.md) |
| USGS Water — NWIS / Water Data | `usgs-water`, `usgs-nwis`, `nwis` | tabular | none | `usgs-water` | [USGS Water](usgs-water/introduction.md) |
| Caravan large-sample hydrology (incl. the open GRDC subset) | `caravan`, `caravan-grdc`, `grdc-caravan` | tabular | none (CC-BY-4.0) | — | [Caravan](caravan/introduction.md) |
| JRC Global Human Settlement Layer | `ghsl`, `ghs`, `ghsl:human-settlement` | raster | none (open HTTPS) | — | [GHSL](ghsl/introduction.md) |
| WorldPop population data hub | `worldpop`, `world-pop` | mixed (rasters + age/sex tables) | none (CC-BY-4.0) | `worldpop` (optional) | [WorldPop](worldpop/introduction.md) |
| Alaska Satellite Facility (SAR search + InSAR baselines) | `asf`, `alaska-satellite-facility`, `asf:insar` | raster | NASA Earthdata Login (reuses `earthdata`) | `asf` | [ASF](asf/introduction.md) |
| JAXA Earth-observation archive (jaxa-earth STAC/COG + G-Portal SFTP) | `jaxa`, `jaxa-earth`, `g-portal` | raster | none (jaxa-earth) / G-Portal SFTP credentials | `jaxa` | [JAXA](jaxa/introduction.md) |
| GBIF species occurrences | `gbif` | vector | none (anonymous) | `gbif` | [GBIF](gbif/introduction.md) |
| OBIS marine occurrences | `obis` | vector | none (anonymous) | `obis` | [OBIS](obis/introduction.md) |
| Protected Planet (WDPA) protected areas | `wdpa`, `protected-planet` | vector | API token (`?token=`) | — | [WDPA](wdpa/introduction.md) |
| IUCN Red List assessments | `iucn`, `redlist` | tabular | Bearer token | — | [IUCN](iucn/introduction.md) |
| Generic ERDDAP servers (NOAA CoastWatch / Coral Reef Watch / NCEI / …) | `erddap`, `ioos` | per-dataset (raster griddap / tabular tabledap) | none (public servers) | `erddap` | [ERDDAP](erddap/introduction.md) |
| Bathymetry DEMs (GEBCO 2020 / NOAA ETOPO1 ice + bedrock) | `bathymetry`, `gebco`, `etopo` | raster | none (open ERDDAP) | — | [Bathymetry](bathymetry/introduction.md) |
| Argo float ocean profiles | `argo`, `argo-floats`, `argopy` | tabular | none (open data) | `argo` | [Argo](argo/introduction.md) |
| Administrative boundaries (geoBoundaries / CGAZ / Natural Earth / TIGER) | `admin`, `admin-boundaries`, `geoboundaries`, `natural-earth`, `tiger` | vector | none (public) | — | [Administrative boundaries](admin/introduction.md) |
| ISRIC SoilGrids 2.0 soil properties (250 m, OGC WCS) | `soilgrids`, `isric` | raster | none (public, CC-BY 4.0) | — | [SoilGrids](soilgrids/introduction.md) |
| Drought indicators (USDM / Copernicus EDO + GDO / CSIC SPEIbase) | `drought`, `usdm`, `edo`, `gdo` | per-dataset (vector USDM polygons / raster EDO+GDO+SPEIbase) | none | — | [Drought](drought/introduction.md) |
| Climate indices — NOAA PSL teleconnections (ENSO / NAO / PDO / …) | `climate-indices`, `climate_indices`, `climate-indices:teleconnections` | tabular | none (public) | — | [Climate indices](climate_indices/introduction.md) |
| CMIP6 climate projections (Pangeo cloud archive) | `cmip6`, `cmip6:climate-projections`, `pangeo-cmip6` | raster | none (public) | — | [CMIP6](cmip6/introduction.md) |
| Copernicus DEM global land elevation | `dem`, `cop-dem`, `copernicus-dem`, `dem:elevation` | raster | unsigned AWS (public bucket) | `s3` | [DEM](dem/introduction.md) |
| Glacier outlines and mass balance (RGI / GLIMS / WGMS) | `glaciers`, `rgi`, `glims`, `wgms` | vector | none (public) | — | [Glaciers](glaciers/introduction.md) |
| NOAA GOES-R ABI geostationary imagery | `goes` | raster | unsigned AWS (public bucket) | `s3` | [GOES](goes/introduction.md) |
| NREL solar and wind resource (NSRDB / WIND Toolkit) | `nrel`, `nsrdb`, `wind-toolkit` | tabular | API key (`NREL_API_KEY` + `NREL_EMAIL`) | — | [NREL](nrel/introduction.md) |
| OpenStreetMap features (Overpass / ohsome / PBF extracts) | `osm`, `openstreetmap`, `overpass`, `ohsome` | vector | none (public) | `osm` | [OSM](osm/introduction.md) |
| PVGIS solar radiation and PV performance (EU JRC) | `pvgis`, `pvgis:solar-pv` | tabular | none (public) | — | [PVGIS](pvgis/introduction.md) |
| Risk indicators (ThinkHazard! / INFORM / Global Forest Watch) | `risk-indicators`, `thinkhazard`, `inform`, `gfw`, `global-forest-watch` | tabular | none, except GFW (`GFW_API_KEY`) | — | [Risk indicators](risk_indicators/introduction.md) |
| Global Solar Atlas and Global Wind Atlas (World Bank / ESMAP) | `solar-wind-atlas`, `global-solar-atlas`, `global-wind-atlas`, `gsa`, `gwa` | raster | none (public) | — | [Solar & Wind Atlas](solar_wind_atlas/introduction.md) |

`Output` is the backend's `OUTPUT_KIND` — `raster` writes GeoTIFF/COG/NetCDF, `vector` writes geometry tables
(GeoJSON / GeoPackage), `tabular` writes plain tables (CSV / Parquet), and `mixed` (HDX, Sentinel Hub, WorldPop)
covers backends whose products vary by request. It is also the first half of the `aggregate=` gate: the temporal
aggregator is forwarded only when `OUTPUT_KIND` is `raster` **or** `mixed` **and** the backend declares
`SUPPORTS_AGGREGATE`. `vector` / `tabular` backends always refuse, and a raster backend that has not wired the
reducer refuses too.
