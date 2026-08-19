# Data Sources

## Design Concept

earthlens is designed following the Template/Factory design pattern to create an abstract class as a template for different data sources.

The main objective is to provide a unified API for all remote sensing data sources, where you only have to worry about the domain of your data (date range and spatial extent) and the package does everything in the backend.

`earthlens` provides a unified API across **61 providers** — see [All supported providers](#all-supported-providers)
below for the full index, or jump straight to a provider's own reference page
(`docs/reference/<id>/introduction.md`) for its full walkthrough.

!!! note
    Many data sources (Google Earth Engine, ECMWF, EUMETSAT, Sentinel Hub, …) require authentication keys. See the [Authentication](authentication.md) page for setup instructions, or each provider's own reference page for provider-specific credential details.

The API takes a few parameters to determine the domain of your data:

- **Date range**: `start`, `end`, and `temporal_resolution`
- **Spatial extent**: `lat_lim` (latitude limits) and `lon_lim` (longitude limits)
- If `lat_lim` and `lon_lim` are not provided, the `EarthLens` class defaults to longitude `[-180, 180]` and latitude `[-90, 90]`.

```python
from earthlens.core import EarthLens

start = "2009-01-01"
end = "2009-01-10"
temporal_resolution = "daily"
latlim = [4.19, 4.64]
lonlim = [-75.65, -74.73]
```

Each data source has different climate variables/datasets. To discover available variables, use the `Catalog` class for each data source (see [Data Catalog](catalog.md)).

!!! info
    The downloaded data format differs based on the data source. CHIRPS and ECMWF have a `post_download` function that converts the NetCDF format into GeoTIFF using the [pyramids](https://github.com/serapeum-org/pyramids) GIS package.

!!! note
    In future versions, `lat_lim` and `lon_lim` will be deprecated and replaced by a GeoDataFrame containing a polygon geometry.

## All supported providers

Each provider's `id` links to its own reference page for the full walkthrough, authentication details, and catalog. Pass the `data_source` value to `EarthLens(...)`.

### Air quality

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/airnow.svg" height="20"> | [AirNow (US EPA)](../reference/airnow/introduction.md) | `airnow` |
| <img src="../_images/logos/eea-aq.svg" height="20"> | [European Environment Agency](../reference/eea-aq/introduction.md) | `eea-aq` |
| <img src="../_images/logos/openaq.svg" height="20"> | [OpenAQ](../reference/openaq/introduction.md) | `openaq` |
| <img src="../_images/logos/sensor-community.png" height="20"> | [Sensor.Community](../reference/sensor-community/introduction.md) | `sensor-community` |

### Biodiversity & protected areas

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/gbif.svg" height="20"> | [GBIF](../reference/gbif/introduction.md) | `gbif` |
| <img src="../_images/logos/iucn.svg" height="20"> | [IUCN Red List](../reference/iucn/introduction.md) | `iucn` |
| <img src="../_images/logos/obis.png" height="20"> | [OBIS](../reference/obis/introduction.md) | `obis` |
| <img src="../_images/logos/wdpa.png" height="20"> | [Protected Planet (UNEP-WCMC)](../reference/wdpa/introduction.md) | `wdpa` |

### Climate reanalysis & projections

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/ecmwf.png" height="20"> | [Copernicus Climate Data Store (ECMWF)](../reference/ecmwf/introduction.md) | `ecmwf` |
| <img src="../_images/logos/climate_indices.svg" height="20"> | [NOAA Physical Sciences Laboratory](../reference/climate_indices/introduction.md) | `climate-indices` |
| <img src="../_images/logos/cmip6.svg" height="20"> | [WCRP CMIP6](../reference/cmip6/introduction.md) | `cmip6` |

### Disasters & risk

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/fdsn.png" height="20"> | [FDSN](../reference/fdsn/introduction.md) | `fdsn` |
| <img src="../_images/logos/gdacs.png" height="20"> | [GDACS](../reference/gdacs/introduction.md) | `gdacs` |
|   | [EM-DAT disaster impacts](../reference/emdat/introduction.md) | `emdat`, `gdis` |
| <img src="../_images/logos/firms.png" height="20"> | [NASA FIRMS](../reference/firms/introduction.md) | `firms` |
| <img src="../_images/logos/risk_indicators.svg" height="20"> | [ThinkHazard! (GFDRR/World Bank)](../reference/risk_indicators/introduction.md) | `thinkhazard` |

### Elevation & bathymetry

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/dem.svg" height="20"> | [Copernicus DEM (ESA)](../reference/dem/introduction.md) | `dem` |
| <img src="../_images/logos/bathymetry.png" height="20"> | [GEBCO](../reference/bathymetry/introduction.md) | `bathymetry` |

### Glaciers & cryosphere

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/glaciers.png" height="20"> | [NSIDC Randolph Glacier Inventory](../reference/glaciers/introduction.md) | `glaciers` |

### Humanitarian data

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/hdx.png" height="20"> | [Humanitarian Data Exchange (UN OCHA)](../reference/hdx/introduction.md) | `hdx` |

### Hydrology

| | Provider | `data_source` |
|---|---|---|
|   | [GloH2O MSWEP / MSWX](../reference/mswep/introduction.md) | `mswep`, `mswx` |
|   | [NOAA National Water Model](../reference/nwm/introduction.md) | `nwm` |
| <img src="../_images/logos/usgs-water.svg" height="20"> | [USGS National Water Information System](../reference/usgs-water/introduction.md) | `usgs-water` |

### Multi-mission imagery & data platforms

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/s3.png" height="20"> | [AWS Open Data](../reference/s3/introduction.md) | `amazon-s3` |
| <img src="../_images/logos/eumetsat.svg" height="20"> | [EUMETSAT](../reference/eumetsat/introduction.md) | `eumetsat` |
| <img src="../_images/logos/gee.png" height="20"> | [Google Earth Engine](../reference/gee/introduction.md) | `gee` |
| <img src="../_images/logos/jaxa.svg" height="20"> | [JAXA](../reference/jaxa/introduction.md) | `jaxa` |
| <img src="../_images/logos/earthdata.png" height="20"> | [NASA Earthdata](../reference/earthdata/introduction.md) | `earthdata` |
| <img src="../_images/logos/goes.png" height="20"> | [NOAA GOES-R](../reference/goes/introduction.md) | `goes` |
| <img src="../_images/logos/stac.png" height="20"> | [STAC (SpatioTemporal Asset Catalog)](../reference/stac/introduction.md) | `stac` |
| <img src="../_images/logos/sentinel-hub.png" height="20"> | [Sentinel Hub](../reference/sentinel-hub/introduction.md) | `sentinel-hub` |
| <img src="../_images/logos/openeo.png" height="20"> | [openEO](../reference/openeo/introduction.md) | `openeo` |

### Ocean

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/argo.png" height="20"> | [Argo Program](../reference/argo/introduction.md) | `argo` |
| <img src="../_images/logos/cmems.svg" height="20"> | [Copernicus Marine Service](../reference/cmems/introduction.md) | `cmems` |
| <img src="../_images/logos/erddap.svg" height="20"> | [NOAA ERDDAP](../reference/erddap/introduction.md) | `erddap` |

### Population & human settlement

| | Provider | `data_source` |
|---|---|---|
|   | [European Commission Joint Research Centre (GHSL)](../reference/ghsl/introduction.md) | `ghsl` |
| <img src="../_images/logos/worldpop.png" height="20"> | [WorldPop](../reference/worldpop/introduction.md) | `worldpop` |

### Precipitation & drought

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/chc.png" height="20"> | [Climate Hazards Center (UCSB)](../reference/chc/introduction.md) | `chc` |
| <img src="../_images/logos/drought.svg" height="20"> | [Copernicus European Drought Observatory / NDMC](../reference/drought/introduction.md) | `drought` |

### Renewable energy

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/solar_wind_atlas.svg" height="20"> | [Global Solar Atlas / Global Wind Atlas (World Bank/ESMAP)](../reference/solar_wind_atlas/introduction.md) | `solar-wind-atlas` |
| <img src="../_images/logos/nrel.svg" height="20"> | [National Laboratory of the Rockies (formerly NREL)](../reference/nrel/introduction.md) | `nrel` |
| <img src="../_images/logos/pvgis.svg" height="20"> | [PVGIS (EU JRC)](../reference/pvgis/introduction.md) | `pvgis` |

### SAR / radar imagery

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/asf.png" height="20"> | [Alaska Satellite Facility (ASF)](../reference/asf/introduction.md) | `asf` |

### Soil

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/soilgrids.svg" height="20"> | [ISRIC SoilGrids](../reference/soilgrids/introduction.md) | `soilgrids` |

### Tropical cyclones

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/tropycal.png" height="20"> | [Tropycal](../reference/tropycal/introduction.md) | `tropycal` |

### Vector basemaps & boundaries

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/osm.svg" height="20"> | [OpenStreetMap](../reference/osm/introduction.md) | `osm` |
| <img src="../_images/logos/overture.svg" height="20"> | [Overture Maps Foundation](../reference/overture/introduction.md) | `overture` |
|   | [geoBoundaries](../reference/admin/introduction.md) | `admin` |

### Weather forecast (NWP)

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/nwp.png" height="20"> | [Herbie (NWP archive access)](../reference/nwp/introduction.md) | `nwp` |

### Weather radar

| | Provider | `data_source` |
|---|---|---|
| <img src="../_images/logos/radar.svg" height="20"> | [NOAA NEXRAD](../reference/radar/introduction.md) | `radar` |

Logos are each provider's own mark, used only to identify which service a backend talks to (not
an endorsement of earthlens by that provider) — see
[docs/_images/logos/ATTRIBUTION.md](https://github.com/serapeum-org/earthlens/blob/main/docs/_images/logos/ATTRIBUTION.md)
for sourcing and rights notes on every logo.

## Quick examples

A few backends' end-to-end usage, worked out in full below. Every other provider's own
[introduction](#all-supported-providers) page has the same kind of walkthrough.

## ECMWF (Copernicus Climate Data Store)

The ECMWF backend talks to the Copernicus Climate Data Store via
`cdsapi`. ERA-Interim was retired in 2019 and the public-datasets
endpoint that hosted it was decommissioned in 2023; **ERA5 on CDS is
the production successor** and what every ECMWF retrieve in this
package now hits. Set up your `~/.cdsapirc` first
(see [Authentication](authentication.md)) and accept the licence for
the relevant ERA5 dataset on the CDS website.

```python
source = "ecmwf"
path = "examples/data/era5"
# Variables are addressed by (CDS dataset short name, variable code).
variables = {
    "reanalysis-era5-single-levels": ["2m-temperature"],
}

earthlens = EarthLens(
    data_source=source,
    start=start,
    end=end,
    variables=variables,
    lat_lim=latlim,
    lon_lim=lonlim,
    temporal_resolution=temporal_resolution,
    path=path,
)
earthlens.download()
```

!!! note "Expect to wait"
    `client.retrieve()` blocks until the request reaches the front of
    the CDS queue and the file is generated — typically minutes,
    occasionally longer for large requests. Pick a small bbox and date
    range to keep wait times bearable. In CI the cdsapi client is
    mocked; the live end-to-end suite is selected with `pytest -m e2e`.

## CHC (CHIRPS / CHIRP / CHIRTS / …)

```python
source = "chc"
path = "examples/data/chirps"
variables = ["precipitation"]

earthlens = EarthLens(
    data_source=source,
    start=start,
    end=end,
    variables=variables,
    lat_lim=latlim,
    lon_lim=lonlim,
    temporal_resolution=temporal_resolution,
    path=path,
)
earthlens.download()
```

### Parallel Download

```python
path = "examples/data/chirps-cores"

earthlens = EarthLens(
    data_source=source,
    start=start,
    end=end,
    variables=variables,
    lat_lim=latlim,
    lon_lim=lonlim,
    temporal_resolution=temporal_resolution,
    path=path,
)
earthlens.download(cores=4)
```

## Amazon S3

```python
path = "examples/data/s3-backend"
source = "amazon-s3"
variables = ["precipitation"]

earthlens = EarthLens(
    data_source=source,
    start=start,
    end=end,
    variables=variables,
    temporal_resolution=temporal_resolution,
    path=path,
)
earthlens.download()
```
