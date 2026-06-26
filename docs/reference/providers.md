# Supported data sources

earthlens exposes every provider through one facade, `EarthLens(data_source="<key>", ...)`. The table below lists
the providers that are integrated today, the string key(s) each one accepts, the natural output it produces, and
the optional dependency (extra) you need to install for it.

Install a backend's optional dependency with:

```bash
pip install earthlens[<extra>]      # e.g. earthlens[ecmwf]
pip install earthlens[all]          # every backend's SDK
```

Backends with no extra (CHC, GDACS, FIRMS, OpenAQ, GHSL) need only the core install — they use anonymous FTP or plain HTTP.

## Integrated providers

| Provider | `data_source` key(s) | Output | Auth | Extra | Docs |
|---|---|---|---|---|---|
| Climate Hazards Center (CHIRPS / CHIRTS / SPI / SPEI / WBGT / …) | `chc`, `chirps` | raster | anonymous FTP | — | [CHC](chc/introduction.md) |
| AWS Open Data (ERA5 / Sentinel-2 / Copernicus DEM / ESA WorldCover) | `amazon-s3` | raster | unsigned AWS (public buckets) | `s3` | [Amazon S3](s3/introduction.md) |
| ECMWF Climate Data Store | `ecmwf` | raster | `~/.cdsapirc` token | `ecmwf` | [ECMWF](ecmwf/introduction.md) |
| Google Earth Engine | `gee`, `google-earth-engine` | raster | service account | `gee` | [GEE](gee/introduction.md) |
| Copernicus Marine (CMEMS) | `cmems` | raster | Copernicus Marine login | `cmems` | [CMEMS](cmems/introduction.md) |
| FDSN seismic events (USGS / EMSC / INGV / …) | `fdsn` | vector | none | `fdsn` | [FDSN](fdsn/introduction.md) |
| GDACS disaster alerts | `gdacs` | vector | none | — | [GDACS](gdacs/introduction.md) |
| OpenAQ air quality | `openaq` | tabular | API key (`X-API-Key`) | — | [OpenAQ](openaq/introduction.md) |
| Tropycal tropical-cyclone tracks | `tropycal` | vector (tabular for SHIPS) | none | `tropycal` | [Tropycal](tropycal/introduction.md) |
| STAC — Planetary Computer / CDSE / Earth Search | `stac`, `cdse` | raster | per-endpoint (anonymous / MPC SAS / CDSE S3) | `stac` | [STAC](stac/introduction.md) |
| NASA Earthdata (9 EOSDIS DAACs via `earthaccess`) | `earthdata` | per-dataset (raster / vector / tabular) | EDL login or bearer token | `earthdata` | [Earthdata](earthdata/introduction.md) |
| Open NWP forecasts (NOAA NODD / ECMWF Open Data / DWD via Herbie) | `nwp` | raster | none (open buckets) | `nwp` | [NWP](nwp/introduction.md) |
| NEXRAD Level-II radar (real-time chunk feed) | `radar`, `nexrad` | vector | none (anonymous S3) | `radar` | [NEXRAD radar](radar/introduction.md) |
| openEO server-side processing (defaults to CDSE) | `openeo` | raster | CDSE OIDC (interactive or client-credentials) | `openeo` | [openEO](openeo/introduction.md) |
| NOAA National Water Model (`noaa-nwm-pds`) | `nwm`, `national-water-model` | per-product (`chrtout` tabular / `ldasout` raster) | unsigned AWS (public bucket) | `nwm` | [NWM](nwm/introduction.md) |
| Humanitarian Data Exchange (UN OCHA, CKAN) | `hdx` | mixed | none (public) | `hdx` | [HDX](hdx/introduction.md) |
| NASA FIRMS active fire detections | `firms` | vector | `FIRMS_MAP_KEY` | — | [FIRMS](firms/introduction.md) |
| EUMETSAT Data Store | `eumetsat` | per-dataset (raster default) | consumer key / secret | `eumetsat` | [EUMETSAT](eumetsat/introduction.md) |
| Sentinel Hub server-side render (CDSE) | `sentinel-hub`, `sentinelhub` | mixed (raster / tabular per plane) | OAuth client id / secret | `sentinel-hub` | [Sentinel Hub](sentinel-hub/introduction.md) |
| Overture Maps vector basemap | `overture` | vector | none (public) | `overture` | [Overture](overture/introduction.md) |
| USGS Water — NWIS / Water Data | `usgs-water`, `usgs-nwis`, `nwis` | tabular | none | `usgs-water` | [USGS Water](usgs-water/introduction.md) |
| JRC Global Human Settlement Layer | `ghsl`, `ghs`, `human-settlement` | raster | none (open HTTPS) | — | [GHSL](ghsl/introduction.md) |
| WorldPop population data hub | `worldpop`, `world-pop` | mixed (rasters + age/sex tables) | none (CC-BY-4.0) | `worldpop` (optional) | [WorldPop](worldpop/introduction.md) |
| Alaska Satellite Facility (SAR search + InSAR baselines) | `asf`, `alaska-satellite-facility`, `insar` | vector | NASA Earthdata Login (reuses `earthdata`) | `asf` | [ASF](asf/introduction.md) |
| JAXA Earth-observation archive (jaxa-earth STAC/COG + G-Portal SFTP) | `jaxa`, `jaxa-earth`, `g-portal` | raster | none (jaxa-earth) / G-Portal SFTP credentials | `jaxa` | [JAXA](jaxa/introduction.md) |
| GBIF species occurrences | `gbif` | vector | none (anonymous) | `gbif` | [GBIF](gbif/introduction.md) |
| OBIS marine occurrences | `obis` | vector | none (anonymous) | `obis` | [OBIS](obis/introduction.md) |
| Protected Planet (WDPA) protected areas | `wdpa`, `protected-planet` | vector | API token (`?token=`) | — | [WDPA](wdpa/introduction.md) |
| IUCN Red List assessments | `iucn`, `redlist` | tabular | Bearer token | — | [IUCN](iucn/introduction.md) |
| Generic ERDDAP servers (NOAA CoastWatch / Coral Reef Watch / NCEI / …) | `erddap`, `ioos` | per-dataset (raster griddap / tabular tabledap) | none (public servers) | `erddap` | [ERDDAP](erddap/introduction.md) |
| Bathymetry DEMs (GEBCO 2020 / NOAA ETOPO1 ice + bedrock) | `bathymetry`, `gebco`, `etopo` | raster | none (open ERDDAP) | — | [Bathymetry](bathymetry/introduction.md) |
| Argo float ocean profiles | `argo`, `argo-floats`, `argopy` | tabular | none (open data) | `argo` | [Argo](argo/introduction.md) |

`Output` is the backend's `OUTPUT_KIND` — `raster` writes GeoTIFF/COG/NetCDF, `vector` writes geometry tables
(GeoJSON / GeoPackage), `tabular` writes plain tables (CSV / Parquet), and `mixed` (HDX, Sentinel Hub, WorldPop)
covers backends whose products vary by request. It also governs `aggregate=`: the temporal aggregator is
accepted for `raster` backends and rejected for `vector` / `tabular` ones; the `mixed` backends reject it
because their products are returned as-is.
