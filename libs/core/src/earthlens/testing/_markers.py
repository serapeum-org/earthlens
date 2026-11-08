"""The one marker registry, shared by every member and every lane.

Markers used to be declared twice — in the root `pyproject.toml` (which
`wheel-test.yml` and every `tests-e2e.yml` job read) and again in each member's
own (which every `tests.yml` lane reads). The two sets drifted: `cli` was used
by four members that did not declare it, `real_band_names` only existed at the
root, `aqueduct` and `radklim` only in a member. Each drift degraded silently to
`PytestUnknownMarkWarning`, and a marker that warns is a marker that selects
nothing — in a lane keyed on `-m "e2e and <backend>"`, a silently empty run.

So the list lives here instead, in the installed package every member conftest
already imports, and is registered through `pytest_configure`. One definition,
reachable from any rootdir, with `--strict-markers` turning a typo into a
collection error instead of a warning.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

import pytest

#: Marker name to its `pytest --markers` description.
MARKERS: dict[str, str] = {
    "admin": "tests for the administrative-boundaries backend (geoBoundaries + CGAZ + Natural Earth + US Census TIGER/Line; no extra SDK — core requests + pyramids; all four public, GADM omitted for license).",
    "airnow": "tests for the AirNow air-quality backend (US/Canada EPA `/aq/data/` bbox REST; no extra SDK — core `requests` + `pandas`; needs a free AIRNOW_API_KEY for live e2e).",
    "aqueduct": "tests for the WRI Aqueduct riverine flood-risk backend (files.wri.org 2015 Analyzer, CC-BY-4.0; no extra SDK — core requests + pyramids; country/state/basin exposure shapefiles).",
    "argo": "tests for the Argo float-profile backend (requires the `argo` extra: argopy; open data, no auth).",
    "asf": "tests for the ASF InSAR backend (requires the `asf` extra: asf_search; reuses EDL auth from `earthdata`).",
    "bathymetry": "tests for the bathymetry DEM backend (GEBCO 2020 + ETOPO1 ice/bedrock via NOAA ERDDAP griddap; EMODnet Bathymetry European DTM via OGC WCS; no extra SDK — core requests + pyramids).",
    "biodiversity": "tests for the shared biodiversity-cluster helpers (`wkt_from_bbox`, `occurrences_to_fc`, `LicenseWarning`; no extra SDK).",
    "caravan": "tests for the Caravan large-sample hydrology backend (no extra SDK; static Zenodo archives read over HTTP Range, includes the open GRDC-Caravan subset).",
    "catrare": "tests for the CatRaRE DWD heavy-rainfall event-catalogue backend (opendata.dwd.de FileGDB v2026.01, CC-BY-4.0 / GeoNutzV, no extra SDK — core HttpClient + pyramids; RADKLIM-derived events reprojected from the RADOLAN grid).",
    "chc": "tests for the Climate Hazards Center (CHC) FTP backend (no extra SDK; covers CHIRPS / CHIRP / CHIRTS / WBGT / SPI / SPEI / CHC_CMIP6 etc.).",
    "cli": "tests for the `earthlens` console script / federated catalog-query CLI (typer and rich are core dependencies; no extra needed).",
    "climate_indices": "tests for the climate / teleconnection index backend (NOAA PSL + KNMI Climate Explorer monthly ASCII series; no extra SDK — core requests + pandas).",
    "cmems": "tests for the Copernicus Marine backend (requires the `cmems` extra: copernicusmarine).",
    "cmip6": "tests for the CMIP6 backend (raw archive on the open Pangeo gs://cmip6 bucket; anonymous, no extra SDK; reads via pyramids).",
    "dem": "tests for the Copernicus DEM backend (GLO-30 / GLO-90; requires the `dem` extra: boto3; anonymous copernicus-dem-30m / -90m buckets; raw COG tiles — no decode, mosaic/crop is pyramids downstream).",
    "drought": "tests for the drought backend (US Drought Monitor / Copernicus EDO+GDO / CSIC SPEIbase; no extra SDK — core `requests` + pyramids; EDO/GDO fetched via the Copernicus GetCoverage REST endpoint, SPEIbase via direct NetCDF).",
    "e2e": "end-to-end test against a live external service (e.g. CDS); select with `-m e2e`.",
    "earthdata": "tests for the NASA Earthdata backend (requires the `earthdata` extra: earthaccess; Python >=3.12).",
    "ecmwf": "tests for the ECMWF/CDS backend (requires the `ecmwf` extra: cdsapi).",
    "eea": "tests for the EEA (`eea_aq`) air-quality backend (requires the `eea_aq` extra: airbase; European monitor observations via Parquet; public, no auth).",
    "emdat": "tests for the EM-DAT disaster backend (the UCLouvain Dataverse archive is anonymous and needs no extra; the GDIS sources need the `emdat` extra: earthaccess, plus an Earthdata Login).",
    "erddap": "tests for the generic ERDDAP backend (requires the `erddap` extra: erddapy; griddap raster via direct .nc download + pyramids, tabledap tabular via to_pandas; public servers only).",
    "eumetsat": "tests for the EUMETSAT Data Store backend (requires the `eumetsat` extra: eumdac).",
    "fabdem": "tests for the FABDEM V1-2 bare-earth DEM backend (Forest And Buildings removed Copernicus DEM, ~30m; open HTTPS from the University of Bristol, no extra SDK — core requests + pyramids; CC-BY-NC-SA 4.0 non-commercial).",
    "fdsn": "tests for the FDSN seismic-event backend (requires the `fdsn` extra: obspy).",
    "firms": "tests for the NASA FIRMS active-fire backend (free MAP_KEY, no extra SDK; uses the core `requests` + `pandas` dependencies).",
    "flodis": "tests for the FLODIS observed-flood footprints<->impacts backend (public Zenodo record 8123096, CC-BY-4.0, no extra SDK — core HttpClient + pandas; global companion to hanze, sibling of emdat).",
    "flopros": "tests for the FLOPROS flood-protection-standard backend (NHESS-2016 supplement shapefile, CC-BY-3.0, no extra SDK — core HttpClient + base/archive + pyramids; subnational protection standards as return periods).",
    "gbif": "tests for the GBIF species-occurrence backend (requires the `gbif` extra: pygbif; search is anonymous).",
    "gdacs": "tests for the GDACS multi-hazard alert backend (public feed, no extra SDK).",
    "gdis": "tests for the GDIS half of the EM-DAT backend (needs an Earthdata Login plus acceptance of the SEDAC data-use agreement); deselect with `-m \"not gdis\"` on a machine without credentials.",
    "gee": "tests for the Google Earth Engine backend (requires the `gee` extra: earthengine-api).",
    "ghsl": "tests for the GHSL backend (JRC Global Human Settlement Layer; open HTTPS, no extra SDK for the direct path; localised via pyramids).",
    "glaciers": "tests for the glaciers backend (RGI 7.0 per-region outlines via IHP-WINS + GLIMS WFS time-series outlines + WGMS Fluctuations of Glaciers tabular; no extra SDK — core requests + pandas + pyramids; mixed per-instance OUTPUT_KIND; all sources open, no auth).",
    "goes": "tests for the NOAA GOES-R ABI backend (requires the `goes` extra: boto3; unsigned noaa-goes19/18/16 buckets).",
    "hanze": "tests for the HANZE historical European flood-impacts backend (public Zenodo record 20478847, CC-BY-4.0, no extra SDK — core HttpClient + pandas + pyramids).",
    "hdx": "tests for the Humanitarian Data Exchange backend (requires the `hdx` extra: hdx-python-api).",
    "integration": "exercises multiple modules together with mocked I/O.",
    "isimip": "tests for the ISIMIP bias-adjusted climate-forcing backend (requires the `isimip` extra: isimip-client; facet search + server-side cutout job on the ISIMIP repository, public/no auth).",
    "iucn": "tests for the IUCN Red List v4 backend (no extra SDK; direct v4 REST via core `requests`; needs a Bearer token).",
    "jaxa": "tests for the JAXA backend (three protocols — authless STAC/COG via jaxa.earth, credentialed SFTP via gportal, credentialed FTP via P-Tree/Himawari; the jaxa-earth and gportal branches need the `jaxa` extra, the ptree branch uses stdlib `ftplib` only).",
    "jrc": "tests for the JRC hazards backend — the European Flood Hazard Map (river-flood water depth per return period, Europe + Mediterranean) and the probabilistic sea-level (Total Water Level) forecasts (global 0.25 deg gridded cubes + a per-country coastal summary); open HTTPS from the JRC, no extra SDK — core requests + pyramids `/vsicurl` windowed reads; CC-BY 4.0).",
    "mswep": "tests for the GloH2O MSWEP / MSWX backend (requires the `mswep` extra: google-api-python-client, google-auth; downloads NetCDF granules from the user's approved GloH2O Google-Drive share -- CC-BY-NC, access is a prerequisite).",
    "nrel": "tests for the NREL NSRDB / WIND Toolkit solar-wind time-series backend (keyed REST CSV via core `requests` + `pandas`; no extra SDK; required api_key + email auth).",
    "nsi": "tests for the NSI US flood exposure/loss backend (USACE National Structure Inventory + FEMA NFHL flood zones + FEMA NFIP claims v3; no extra SDK — core requests + pandas + pyramids; per-instance OUTPUT_KIND; all three keyless US public domain).",
    "nwm": "tests for the NOAA National Water Model backend (requires the `nwm` extra: boto3; unsigned noaa-nwm-pds bucket).",
    "nwp": "tests for the NWP forecast backend (requires the `nwp` extra: herbie-data, ecmwf-opendata; GRIB read via pyramids' GDAL driver).",
    "obis": "tests for the OBIS marine-occurrence backend (requires the `obis` extra: pyobis; search is anonymous).",
    "openaq": "tests for the OpenAQ v3 air-quality backend (no extra SDK; uses the core `requests` dependency).",
    "openeo": "tests for the openEO server-side-processing backend (requires the `openeo` extra: openeo).",
    "osm": "tests for the OpenStreetMap backend (requires the `osm` extra: overpy + ohsome; two public keyless protocols — Overpass current-state via overpy, OSM history via ohsome; the non-e2e suite fakes both SDKs).",
    "osm_pbf": "tests for the OpenStreetMap `pbf` protocol (bulk Geofabrik `.osm.pbf` reads; the `osm` extra ships the default streaming pyosmium engine (osmium), while the in-memory pyrosm engine is opt-in via a manual `pip install pyrosm`; the non-e2e suite fakes the reader + HttpClient, the gated e2e downloads a small extract and installs pyrosm to exercise its engine).",
    "overture": "tests for the Overture Maps vector backend (requires the `overture` extra: overturemaps).",
    "ptree": "e2e-only selector for the live P-Tree Himawari HSD FTP fetch (needs $JAXA_PTREE_USERNAME + $JAXA_PTREE_PASSWORD; 30-day rolling archive; the no-network ptree unit tests carry `@pytest.mark.jaxa + @pytest.mark.unit` instead so `-m ptree` does not pick them up).",
    "pvgis": "tests for the JRC PVGIS solar-radiation / PV time-series backend (keyless REST via core `requests` + `pandas`; no extra SDK).",
    "quarantined": "a live test whose upstream is chronically unreliable: held back from every lane by default and run in its own non-blocking one; select with `-m quarantined`.",
    "radar": "tests for the NEXRAD Level-II radar backend (requires the `radar` extra: boto3; unsigned unidata-nexrad-level2-chunks bucket).",
    "radklim": "tests for the DWD RADKLIM / RADOLAN radar-precipitation backend (no extra SDK — core requests + pyramids; anonymous DWD Open Data HTTPS; RADKLIM yearly NetCDF archives + operational HDF5/binary granules over Germany).",
    "real_band_names": "JRC tests that exercise the real band-naming helper instead of the offline stub.",
    "requires_host": "`@pytest.mark.requires_host(\"host\" | \"host:port\" | \"https://url\", timeout=10.0)` - probe the target once per session and skip, with the masked-lane guard prefix, when it does not answer.",
    "risk_indicators": "tests for the risk-indicators backend (GFDRR ThinkHazard! + INFORM Risk + Global Forest Watch; no extra SDK — core requests + pandas + pyramids; GFW datasets need GFW_API_KEY, ThinkHazard/INFORM are public).",
    "s3": "tests for the S3 backend (requires the `s3` extra: boto3).",
    "sensor_community": "tests for the Sensor.Community air-quality backend (crowdsourced low-cost sensors; no extra SDK — core `requests` + `pandas`; public archive + live JSON API, ODbL).",
    "sentinel_hub": "tests for the Sentinel Hub server-side-render backend (requires the `sentinel-hub` extra: sentinelhub).",
    "slow": "mark test as slow.",
    "soilgrids": "tests for the SoilGrids backend (ISRIC SoilGrids 2.0 soil properties via OGC WCS at maps.isric.org; no extra SDK — core requests + pyramids' Dataset.from_wcs; all public, CC-BY 4.0, no auth).",
    "solar_wind_atlas": "tests for the Solar & Wind Atlas backend (Global Solar Atlas + Global Wind Atlas climatology layers; no extra SDK — core requests + pyramids; GWA windowed /vsicurl COG read, GSA download-then-localise).",
    "stac": "tests for the STAC backend (requires the `stac` extra: pyramids-core[stac] → pystac-client + stac-asset).",
    "tropycal": "tests for the Tropycal cyclone-track backend (requires the `tropycal` extra: tropycal, cartopy).",
    "unit": "in-process unit test, no network or filesystem outside tmp_path.",
    "usgs_water": "tests for the USGS NWIS / Water Data backend (requires the `usgs-water` extra: dataretrieval; anonymous access works).",
    "wdpa": "tests for the Protected Planet (WDPA) backend (no extra SDK; direct v4 REST via core `requests`; needs a token).",
    "worldpop": "tests for the WorldPop population data hub backend (open CC-BY-4.0; default REST path needs only core requests + pyramids, optional `worldpop` extra adds worldpoppy).",
}


def pytest_configure(config: pytest.Config) -> None:
    """Register every shared marker, so `--strict-markers` can be on everywhere.

    Args:
        config: The session config to register the markers on.
    """
    for name, description in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")
