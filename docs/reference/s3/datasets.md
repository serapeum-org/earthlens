# Amazon S3 — available datasets

The curated registry (rebuilt/validated by `tools/s3/refresh_s3_catalog.py`). Select a dataset with `dataset="<name>"`; resolve variables by friendly name, alias, or raw native token. Datasets marked **requester-pays** need valid AWS credentials and bill the caller (see [Authentication](authentication.md)).

## `copernicus-dem`

Copernicus GLO-30 global digital elevation model (copernicus-dem-30m), Cloud-Optimised GeoTIFFs on a 1-degree lat/lon tile grid, WGS84. Static (no time axis). The 90 m product (copernicus-dem-90m) is selectable.

- **Bucket**: `copernicus-dem-30m` · **Format**: cog · **CRS**: EPSG:4326 · **Temporal**: static (static)
- **Default variables**: `elevation`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `elevation` | `dem`, `dsm`, `height` | `DEM` | m | Ellipsoidal height (GLO-30 DSM). |

## `era5`

ECMWF ERA5 global reanalysis on NSF NCAR's public mirror (nsf-ncar-era5), hourly data in monthly NetCDF granules, 1940-present, 0.25deg WGS84. Replaces the deprecated era5-pds bucket.

- **Bucket**: `nsf-ncar-era5` · **Format**: netcdf · **CRS**: EPSG:4326 · **Temporal**: temporal (monthly)
- **Default variables**: `t2m`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `t2m` | `2t`, `2m_temperature`, `air_temperature_at_2_metres` | `128_167_2t` | K | 2 metre temperature. |
| `d2m` | `2d`, `2m_dewpoint_temperature` | `128_168_2d` | K | 2 metre dewpoint temperature. |
| `sp` | `surface_pressure` | `128_134_sp` | Pa | Surface pressure. |
| `msl` | `mean_sea_level_pressure`, `air_pressure_at_mean_sea_level` | `128_151_msl` | Pa | Mean sea level pressure. |
| `u10` | `10u`, `10m_u_component_of_wind`, `eastward_wind_at_10_metres` | `128_165_10u` | m s-1 | 10 metre U wind component. |
| `v10` | `10v`, `10m_v_component_of_wind`, `northward_wind_at_10_metres` | `128_166_10v` | m s-1 | 10 metre V wind component. |
| `sst` | `sea_surface_temperature` | `128_034_sstk` | K | Sea surface temperature. |
| `skt` | `skin_temperature` | `128_235_skt` | K | Skin temperature. |
| `tcc` | `total_cloud_cover` | `128_164_tcc` | (0-1) | Total cloud cover. |
| `sd` | `snow_depth` | `128_141_sd` | m of water equivalent | Snow depth. |

## `esa-worldcover`

ESA WorldCover 10 m global land-cover Cloud-Optimised GeoTIFFs (esa-worldcover), 3-degree tile grid, WGS84. Static; two epochs - 2020 (v100) and 2021 (v200). Single-band categorical map.

- **Bucket**: `esa-worldcover` · **Format**: cog · **CRS**: EPSG:4326 · **Temporal**: static (static)
- **Default variables**: `map`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `map` | `landcover`, `land_cover`, `lc` | `Map` | class | 11-class land-cover map (ESA WorldCover legend). |

## `goes`

NOAA GOES-16/18 ABI imagery (noaa-goes16 / noaa-goes18), NetCDF, the Americas, near-real-time. Default product ABI-L2-CMIPF (cloud & moisture imagery, full disk). Geostationary projection (reprojected to the AOI on crop).

- **Bucket**: `noaa-goes16` · **Format**: netcdf · **CRS**: per-file (reprojected) · **Temporal**: temporal (scene)
- **Default variables**: `C02`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `C01` | `blue` | `C01` | reflectance | ABI channel 1, 0.47 um (blue). |
| `C02` | `red` | `C02` | reflectance | ABI channel 2, 0.64 um (red). |
| `C03` | `veggie` | `C03` | reflectance | ABI channel 3, 0.86 um (veggie/NIR). |
| `C07` | `shortwave_window` | `C07` | K | ABI channel 7, 3.9 um. |
| `C08` | `upper_water_vapour` | `C08` | K | ABI channel 8, 6.2 um. |
| `C09` | `mid_water_vapour` | `C09` | K | ABI channel 9, 6.9 um. |
| `C10` | `lower_water_vapour` | `C10` | K | ABI channel 10, 7.3 um. |
| `C13` | `clean_longwave_window` | `C13` | K | ABI channel 13, 10.3 um. |
| `C14` | `longwave_window` | `C14` | K | ABI channel 14, 11.2 um. |

## `naip-source` — **requester-pays**

USDA NAIP aerial imagery 4-band (RGB+NIR) COGs (naip-source, us-east-1), US only. REQUESTER-PAYS: needs valid AWS credentials and bills the caller. Addressed by an explicit quad-object path (tile=, e.g. al/2021/100cm/rgbir_cog/30086/m_3008601_ne_16_060_20211004) - bbox->USGS quarter-quad discovery is out of scope. Per-quad UTM CRS (reprojected on crop).

- **Bucket**: `naip-source` · **Format**: cog · **CRS**: per-file (reprojected) · **Region**: us-east-1 · **Temporal**: temporal (quad)
- **Default variables**: `rgbir`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `rgbir` | `naip`, `rgb_nir` | `rgbir` | reflectance | 4-band NAIP COG (red, green, blue, NIR). |

## `sentinel-2-l2a`

Sentinel-2 Level-2A surface reflectance Cloud-Optimised GeoTIFFs (Element84 Earth Search, sentinel-cogs), global, 2017-present. One COG per band, organised by MGRS tile. Per-tile UTM CRS (reprojected to the AOI on crop).

- **Bucket**: `sentinel-cogs` · **Format**: cog · **CRS**: per-file (reprojected) · **Temporal**: temporal (scene)
- **Default variables**: `B04`, `B03`, `B02`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `B01` | `coastal` | `B01` | reflectance | Coastal aerosol (60 m). |
| `B02` | `blue` | `B02` | reflectance | Blue (10 m). |
| `B03` | `green` | `B03` | reflectance | Green (10 m). |
| `B04` | `red` | `B04` | reflectance | Red (10 m). |
| `B05` | `rededge1` | `B05` | reflectance | Red edge 1 (20 m). |
| `B06` | `rededge2` | `B06` | reflectance | Red edge 2 (20 m). |
| `B07` | `rededge3` | `B07` | reflectance | Red edge 3 (20 m). |
| `B08` | `nir` | `B08` | reflectance | NIR (10 m). |
| `B8A` | `nir08`, `narrow_nir` | `B8A` | reflectance | Narrow NIR (20 m). |
| `B09` | `water_vapour` | `B09` | reflectance | Water vapour (60 m). |
| `B11` | `swir16` | `B11` | reflectance | SWIR 1.6 um (20 m). |
| `B12` | `swir22` | `B12` | reflectance | SWIR 2.2 um (20 m). |
| `SCL` | `scene_classification` | `SCL` | class | Scene classification map (20 m). |

## `usgs-landsat` — **requester-pays**

USGS Landsat Collection-2 Level-2 surface-reflectance / surface-temperature COGs (usgs-landsat, us-west-2). REQUESTER-PAYS: needs valid AWS credentials and bills the caller. Addressed by an explicit Collection-2 scene id (scene=, e.g. LC08_L2SP_039037_20210901_20210910_02_T1) - bbox->WRS-2 path/row discovery is out of scope (use the STAC backend). Per-scene UTM CRS (reprojected to the AOI on crop).

- **Bucket**: `usgs-landsat` · **Format**: cog · **CRS**: per-file (reprojected) · **Region**: us-west-2 · **Temporal**: temporal (scene)
- **Default variables**: `SR_B4`, `SR_B3`, `SR_B2`

| Variable | Aliases | Native token | Units | Description |
|---|---|---|---|---|
| `SR_B1` | `coastal` | `SR_B1` | reflectance | Coastal/aerosol surface reflectance. |
| `SR_B2` | `blue` | `SR_B2` | reflectance | Blue surface reflectance. |
| `SR_B3` | `green` | `SR_B3` | reflectance | Green surface reflectance. |
| `SR_B4` | `red` | `SR_B4` | reflectance | Red surface reflectance. |
| `SR_B5` | `nir`, `nir08` | `SR_B5` | reflectance | NIR surface reflectance. |
| `SR_B6` | `swir16` | `SR_B6` | reflectance | SWIR 1.6 um surface reflectance. |
| `SR_B7` | `swir22` | `SR_B7` | reflectance | SWIR 2.2 um surface reflectance. |
| `ST_B10` | `lst`, `surface_temperature` | `ST_B10` | K | Surface temperature (thermal). |
| `QA_PIXEL` | `qa`, `pixel_qa` | `QA_PIXEL` | bitmask | Pixel quality assessment bitmask. |
