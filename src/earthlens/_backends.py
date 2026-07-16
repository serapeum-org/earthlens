"""Registered backend table and entry-point discovery for the `EarthLens` facade.

This module is deliberately **import-light**: it holds only a plain data table
and the `importlib.metadata` lookup, and imports no provider SDK. That is what
lets `EarthLens.DataSources` stay lazy — resolving an entry point costs one
small module import, never a backend's optional dependency.
"""

from __future__ import annotations

from importlib.metadata import entry_points

#: Group under which a provider distribution publishes its backend table.
#: Each entry point resolves to a `dict[str, BackendSpec]` that is merged into
#: the facade's registry, so a provider package registers all of its keys with
#: a single entry.
ENTRY_POINT_GROUP = "earthlens.backends"

#: `key -> (module, class_name, extras_hint, default_kwargs)`.
#:
#: `extras_hint` names the pip extra that supplies the backend's SDK (empty when
#: it needs none); `default_kwargs` pre-binds constructor arguments for alias
#: keys (e.g. the STAC `"cdse"` alias binds `endpoint="cdse"`). Neither can be
#: expressed by an entry point's `module:attr` target, which is why an entry
#: point resolves to this whole mapping rather than to a backend class.
BackendSpec = tuple[str, str, str, dict[str, object]]

BACKENDS: dict[str, BackendSpec] = {
    "chc": ("earthlens.chc", "CHIRPS", "", {}),
    # Back-compat alias: the package was originally named after
    # its best-known dataset (CHIRPS), then generalised to cover
    # the full Climate Hazards Center catalog. The `"chirps"`
    # key is kept for callers that still use it.
    "chirps": ("earthlens.chc", "CHIRPS", "", {}),
    "amazon-s3": ("earthlens.s3", "S3", "s3", {}),
    # Alaska Satellite Facility SAR search + InSAR baseline `stack()`
    # via `asf_search`. Reuses NASA Earthdata Login from
    # `earthlens.earthdata` — no second credential system. Aliases
    # `"alaska-satellite-facility"` / `"insar"`.
    "asf": ("earthlens.asf", "ASF", "asf", {}),
    "alaska-satellite-facility": ("earthlens.asf", "ASF", "asf", {}),
    "insar": ("earthlens.asf", "ASF", "asf", {}),
    "cmems": ("earthlens.cmems", "CMEMS", "cmems", {}),
    # Raw CMIP6 archive (full model x scenario x variable x member
    # matrix) as analysis-ready Zarr on the open Pangeo `gs://cmip6`
    # bucket. Anonymous (no auth); reads via pyramids (GDAL /vsigs/),
    # so no per-backend SDK — the `cmip6` extra is empty. Aliases
    # `"pangeo-cmip6"` / `"climate-projections"`.
    "cmip6": ("earthlens.cmip6", "CMIP6", "", {}),
    "pangeo-cmip6": ("earthlens.cmip6", "CMIP6", "", {}),
    "climate-projections": ("earthlens.cmip6", "CMIP6", "", {}),
    "earthdata": ("earthlens.earthdata", "Earthdata", "earthdata", {}),
    "ecmwf": ("earthlens.ecmwf", "ECMWF", "ecmwf", {}),
    "eumetsat": ("earthlens.eumetsat", "EUMETSAT", "eumetsat", {}),
    "fdsn": ("earthlens.fdsn", "FDSN", "fdsn", {}),
    "gee": ("earthlens.gee", "GEE", "gee", {}),
    "google-earth-engine": ("earthlens.gee", "GEE", "gee", {}),
    # NOAA GOES-R ABI imagery (anonymous noaa-goes19/18/16 buckets);
    # rides the [s3] extra (unsigned boto3). Raw NetCDF granules out
    # (raster); decode is downstream (pyramids / satpy).
    "goes": ("earthlens.goes", "GOES", "s3", {}),
    # GDACS is a public feed (requests only), so no extra to hint.
    "gdacs": ("earthlens.gdacs", "GDACS", "", {}),
    "hdx": ("earthlens.hdx", "HDX", "hdx", {}),
    "openaq": ("earthlens.openaq", "OpenAQ", "openaq", {}),
    # Ground-obs air-quality trio completing OpenAQ's coverage, all
    # tabular (DataFrame). airnow (US/Canada EPA, /aq/data/ bbox REST)
    # and sensor-community (crowdsourced archive CSV) are core
    # (requests + pandas); eea-aq wraps airbase behind the [eea_aq]
    # extra.
    "airnow": ("earthlens.airnow", "AirNow", "", {}),
    "eea-aq": ("earthlens.eea_aq", "EEA_AQ", "eea_aq", {}),
    "sensor-community": (
        "earthlens.sensor_community",
        "SensorCommunity",
        "",
        {},
    ),
    # openEO server-side processing (defaults to CDSE openEO). Builds a
    # process graph the backend executes; returns the written paths.
    "openeo": ("earthlens.openeo", "OpenEO", "openeo", {}),
    # Sentinel Hub server-side render on CDSE. Builds a bbox/geometry +
    # evalscript request the server renders; returns written GeoTIFF
    # paths (raster planes) or table paths / S3 URIs (tabular / batch).
    # `OUTPUT_KIND="mixed"`. The `"sentinelhub"` alias matches the SDK
    # spelling.
    "sentinel-hub": (
        "earthlens.sentinel_hub",
        "SentinelHub",
        "sentinel-hub",
        {},
    ),
    "sentinelhub": (
        "earthlens.sentinel_hub",
        "SentinelHub",
        "sentinel-hub",
        {},
    ),
    # Overture Maps GeoParquet over public S3 (no creds). Vector
    # FeatureCollection output with a per-row license_id column.
    "overture": ("earthlens.overture", "Overture", "overture", {}),
    # JRC Global Human Settlement Layer (open HTTPS, attribution-only).
    # Download-and-localise raster: tiles/whole-globe .zip -> pyramids
    # reproject/mosaic/crop. No extra SDK (requests + pyramids are core),
    # so no extra to hint. Aliases "ghs" / "human-settlement".
    "ghsl": ("earthlens.ghsl", "GHSL", "", {}),
    "ghs": ("earthlens.ghsl", "GHSL", "", {}),
    "human-settlement": ("earthlens.ghsl", "GHSL", "", {}),
    "tropycal": ("earthlens.tropycal", "TropicalCyclone", "tropycal", {}),
    # FIRMS needs a free MAP_KEY but no SDK (requests + pandas
    # are core), so like GDACS there is no extra to hint.
    "firms": ("earthlens.firms", "FIRMS", "", {}),
    # NOAA National Water Model (anonymous noaa-nwm-pds bucket); the
    # [nwm] extra pulls boto3. Alias "national-water-model".
    "nwm": ("earthlens.nwm", "NWM", "nwm", {}),
    "national-water-model": ("earthlens.nwm", "NWM", "nwm", {}),
    # Open NWP forecasts (NOAA NODD / ECMWF Open Data / DWD); the
    # [nwp] extra pulls herbie-data + ecmwf-opendata.
    "nwp": ("earthlens.nwp", "NWP", "nwp", {}),
    # NEXRAD Level-II radar (anonymous chunk bucket); alias "nexrad".
    "radar": ("earthlens.radar", "Radar", "radar", {}),
    "nexrad": ("earthlens.radar", "Radar", "radar", {}),
    # One unified STAC backend over several endpoints. The bare
    # `"stac"` key leaves the endpoint to be inferred from the
    # requested collection; the three endpoint aliases pre-bind
    # `endpoint=` so `data_source="cdse"` needs no extra kwarg.
    "stac": ("earthlens.stac", "STAC", "stac", {}),
    "planetary-computer": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "planetary-computer"},
    ),
    "earth-search": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "earth-search"},
    ),
    "cdse": ("earthlens.stac", "STAC", "stac", {"endpoint": "cdse"}),
    # Digital Earth Africa STAC (anonymous, af-south-1) — WOfS, FC,
    # crop mask, Landsat/Sentinel-2 ARD, GeoMedian, Copernicus DEM.
    "deafrica": ("earthlens.stac", "STAC", "stac", {"endpoint": "deafrica"}),
    "digital-earth-africa": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "deafrica"},
    ),
    # Digital Earth Australia STAC (anonymous, ap-southeast-2) —
    # Landsat / Sentinel-2 NBART ARD, WOfS, FC, GeoMedian, Intertidal,
    # mangrove cover, SRTM DEM.
    "dea": ("earthlens.stac", "STAC", "stac", {"endpoint": "dea"}),
    "digital-earth-australia": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "dea"},
    ),
    # NASA VEDA STAC (anonymous, us-west-2) — NASA-curated derived
    # products: Black Marble HD nightlights, CMIP6 climate, NLDAS-3,
    # fire/disaster-damage, HLS NDVI, EPA emissions.
    "veda": ("earthlens.stac", "STAC", "stac", {"endpoint": "veda"}),
    # USGS LandsatLook (the authoritative Landsat C2 STAC; SR/ST/L1
    # split into separate collections, requester-pays on
    # s3://usgs-landsat, us-west-2). Alias 'landsat' for convenience.
    "usgs-landsat": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "usgs-landsat"},
    ),
    "landsat": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "usgs-landsat"},
    ),
    # INPE Brazil Data Cube (BDC) STAC v1 — anonymous, the only global
    # source of CBERS-4/4A and AMAZONIA-1. Alias 'brazil-data-cube'.
    "bdc": ("earthlens.stac", "STAC", "stac", {"endpoint": "bdc"}),
    "brazil-data-cube": (
        "earthlens.stac",
        "STAC",
        "stac",
        {"endpoint": "bdc"},
    ),
    # USGS NWIS / Water Data (dataretrieval). Tabular DataFrame of
    # per-site water observations; anonymous access works. The
    # "usgs-nwis" / "nwis" aliases point at the same backend.
    "usgs-water": ("earthlens.usgs_water", "USGSWater", "usgs-water", {}),
    "usgs-nwis": ("earthlens.usgs_water", "USGSWater", "usgs-water", {}),
    "nwis": ("earthlens.usgs_water", "USGSWater", "usgs-water", {}),
    # WorldPop open population data hub (CC-BY-4.0, no creds). Mosaic +
    # crop per-country GeoTIFFs to the AOI; demographic products also
    # emit a tidy age/sex table. `OUTPUT_KIND="mixed"`. Alias
    # "world-pop". The default REST path needs no extra SDK.
    "worldpop": ("earthlens.worldpop", "WorldPop", "worldpop", {}),
    "world-pop": ("earthlens.worldpop", "WorldPop", "worldpop", {}),
    # Biodiversity cluster. GBIF / OBIS are anonymous occurrence search
    # (vector FeatureCollection of points); WDPA returns protected-area
    # polygons (token, ?token= query param); IUCN returns Red List
    # assessments (tabular DataFrame, Bearer token). GBIF/OBIS need the
    # pygbif/pyobis extra; WDPA/IUCN use core requests (no extra).
    "gbif": ("earthlens.gbif", "GBIF", "gbif", {}),
    "obis": ("earthlens.obis", "OBIS", "obis", {}),
    "wdpa": ("earthlens.wdpa", "WDPA", "", {}),
    "protected-planet": ("earthlens.wdpa", "WDPA", "", {}),
    "iucn": ("earthlens.iucn", "IUCN", "", {}),
    "redlist": ("earthlens.iucn", "IUCN", "", {}),
    # JAXA archive over three protocols: authless `jaxa-earth`
    # (STAC + COG via the official jaxa.earth API), credentialed
    # `gportal` (G-Portal SFTP via the community gportal SDK), and
    # credentialed `ptree` (Himawari-8/9 HSD via plain FTP with
    # stdlib ftplib). Per-dataset routing — the catalog's
    # `protocol:` field picks the branch. `OUTPUT_KIND="raster"`.
    "jaxa": ("earthlens.jaxa", "JAXA", "jaxa", {}),
    "jaxa-earth": ("earthlens.jaxa", "JAXA", "jaxa", {}),
    "g-portal": ("earthlens.jaxa", "JAXA", "jaxa", {}),
    # ptree / himawari are unambiguously the stdlib-`ftplib`
    # branch, so their `extras` slot is empty — a failed import
    # here won't misdirect a user to `pip install
    # earthlens[jaxa]` for a branch that never needed it.
    "ptree": ("earthlens.jaxa", "JAXA", "", {}),
    "himawari": ("earthlens.jaxa", "JAXA", "", {}),
    # Argo autonomous-float ocean profiles via the `argopy` SDK
    # (open data, no auth). `OUTPUT_KIND="tabular"` — a long-format
    # DataFrame of profiles. The `"argo"` key is canonical; the
    # `"argo-floats"` / `"argopy"` aliases collapse to it.
    "argo": ("earthlens.argo", "ARGO", "argo", {}),
    "argo-floats": ("earthlens.argo", "ARGO", "argo", {}),
    "argopy": ("earthlens.argo", "ARGO", "argo", {}),
    # Generic ERDDAP client — one backend for many public ERDDAP
    # servers (NOAA CoastWatch / CRW, NCEI, …). `dataset=<id>`
    # picks a curated row; its `protocol` sets the per-instance
    # OUTPUT_KIND (griddap -> raster, tabledap -> tabular). Alias
    # "ioos".
    "erddap": ("earthlens.erddap", "ERDDAP", "erddap", {}),
    "ioos": ("earthlens.erddap", "ERDDAP", "erddap", {}),
    # Global topography / bathymetry DEMs (GEBCO 2020 + ETOPO1
    # ice/bedrock) subset via NOAA ERDDAP griddap -> pyramids ->
    # GeoTIFF. Open data (requests + pyramids are core), so no extra
    # to hint. Aliases "gebco" / "etopo" — pass dataset= to pick the
    # DEM (e.g. dataset="gebco_2020" / "etopo1_ice" / "etopo1_bedrock").
    "bathymetry": ("earthlens.bathymetry", "Bathymetry", "", {}),
    "gebco": ("earthlens.bathymetry", "Bathymetry", "", {}),
    "etopo": ("earthlens.bathymetry", "Bathymetry", "", {}),
    # Global Solar Atlas + Global Wind Atlas climatology layers,
    # bbox-subset to GeoTIFF. Open data / CC-BY-4.0 (requests +
    # pyramids are core), so no extra to hint. The wind layers are
    # read windowed over /vsicurl; the solar layers download once and
    # crop locally. Aliases "global-solar-atlas" / "global-wind-atlas"
    # / "gsa" / "gwa" — pass variables=[...] to pick layers (e.g.
    # variables=["ghi", "wind_100m"]).
    "solar-wind-atlas": (
        "earthlens.solar_wind_atlas",
        "SolarWindAtlas",
        "",
        {},
    ),
    "global-solar-atlas": (
        "earthlens.solar_wind_atlas",
        "SolarWindAtlas",
        "",
        {},
    ),
    "global-wind-atlas": (
        "earthlens.solar_wind_atlas",
        "SolarWindAtlas",
        "",
        {},
    ),
    "gsa": ("earthlens.solar_wind_atlas", "SolarWindAtlas", "", {}),
    "gwa": ("earthlens.solar_wind_atlas", "SolarWindAtlas", "", {}),
    # JRC PVGIS solar-radiation / PV time series over the keyless
    # REST API. Per-coordinate hourly DataFrame (tabular); a point
    # or a bbox sampled to a point grid. variables=["seriescalc"]
    # (hourly radiation / PV power) or ["tmy"]. No extra SDK (core
    # requests + pandas). Alias "solar-pv".
    "pvgis": ("earthlens.pvgis", "PVGIS", "", {}),
    "solar-pv": ("earthlens.pvgis", "PVGIS", "", {}),
    # Monthly climate / teleconnection indices (ENSO/ONI, NAO, AO,
    # PDO, AMO, SOI, PNA, ...) from NOAA PSL + KNMI Climate Explorer
    # ASCII series -> long-format DataFrame. Open data (requests +
    # pandas are core), so no extra to hint. Aliases "climate_indices"
    # / "teleconnections". Global scalar series: spatial args are
    # ignored and aggregate= is rejected.
    "climate-indices": ("earthlens.climate_indices", "ClimateIndices", "", {}),
    "climate_indices": ("earthlens.climate_indices", "ClimateIndices", "", {}),
    "teleconnections": ("earthlens.climate_indices", "ClimateIndices", "", {}),
    # NREL NSRDB (solar) + WIND Toolkit (wind) resource time series over
    # the keyed REST CSV download API. Per-coordinate hourly DataFrame
    # (tabular); a point or a bbox sampled to a point grid. Requires a
    # free NREL api_key + email (NREL_API_KEY / NREL_EMAIL), forwarded
    # via **backend_kwargs. No extra SDK (core requests + pandas). The
    # "nsrdb" / "wind-toolkit" aliases pre-bind product=; pick a product
    # directly with product="nsrdb-psm3" / "nsrdb-tmy" / "wtk".
    "nrel": ("earthlens.nrel", "NREL", "", {}),
    "nsrdb": ("earthlens.nrel", "NREL", "", {"product": "nsrdb-psm3"}),
    "wind-toolkit": ("earthlens.nrel", "NREL", "", {"product": "wtk"}),
    # Country/admin-indexed risk indicators over three sources — GFDRR
    # ThinkHazard! + INFORM Risk (JRC) (both public) + the Global Forest
    # Watch Data API (needs GFW_API_KEY). Per-instance OUTPUT_KIND
    # (tabular -> DataFrame, vector -> FeatureCollection); pass
    # country=<ISO3> (or admin_code=) and, for gfw, api_key=. No extra
    # SDK (core requests + pandas + pyramids). Aliases "thinkhazard" /
    # "inform" / "gfw" / "global-forest-watch".
    "risk-indicators": (
        "earthlens.risk_indicators",
        "RiskIndicators",
        "",
        {},
    ),
    "thinkhazard": ("earthlens.risk_indicators", "RiskIndicators", "", {}),
    "inform": ("earthlens.risk_indicators", "RiskIndicators", "", {}),
    "gfw": ("earthlens.risk_indicators", "RiskIndicators", "", {}),
    "global-forest-watch": (
        "earthlens.risk_indicators",
        "RiskIndicators",
        "",
        {},
    ),
    # Glacier outlines / fluctuations over three open sources — RGI 7.0
    # per-region outlines (UNESCO IHP-WINS) + GLIMS WFS time-series
    # outlines + WGMS Fluctuations of Glaciers (tabular). Per-instance
    # OUTPUT_KIND (vector -> FeatureCollection for rgi/glims, tabular ->
    # DataFrame for wgms); pass a bbox (lat_lim/lon_lim or aoi=) for
    # rgi/glims, or region= / glacier_id= / glacier_name= for wgms. No
    # extra SDK (core requests + pandas + pyramids); no auth. Aliases
    # "rgi" / "glims" / "wgms".
    "glaciers": ("earthlens.glaciers", "Glaciers", "", {}),
    "rgi": ("earthlens.glaciers", "Glaciers", "", {}),
    "glims": ("earthlens.glaciers", "Glaciers", "", {}),
    "wgms": ("earthlens.glaciers", "Glaciers", "", {}),
    # OpenStreetMap features over two public, keyless protocols — overpy
    # (Overpass, current-state) + ohsome (OSM history/analytics). Vector
    # FeatureCollection output with an ODbL LicenseWarning. The [osm]
    # extra pulls overpy + ohsome (imported lazily). Aliases
    # "openstreetmap" / "overpass" / "ohsome".
    "osm": ("earthlens.osm", "OSM", "osm", {}),
    "openstreetmap": ("earthlens.osm", "OSM", "osm", {}),
    "overpass": ("earthlens.osm", "OSM", "osm", {}),
    "ohsome": ("earthlens.osm", "OSM", "osm", {}),
    # Administrative-boundary polygons from four public sources —
    # geoBoundaries (per-country ADM0-5), CGAZ (seamless global ADM0/1/2),
    # Natural Earth (cultural admin), US Census TIGER/Line (states /
    # counties / tracts / nation); GADM omitted for license. Vector
    # FeatureCollection output (EPSG:4326); no extra SDK (core requests +
    # pyramids), all four public. Pass the dataset via variables=
    # ["geoboundaries:adm1"] plus its selector (country=<ISO3> /
    # scale= / year= / state=). Aliases "admin-boundaries" /
    # "geoboundaries" / "natural-earth" / "tiger".
    "admin": ("earthlens.admin", "AdminBoundaries", "", {}),
    "admin-boundaries": ("earthlens.admin", "AdminBoundaries", "", {}),
    "geoboundaries": ("earthlens.admin", "AdminBoundaries", "", {}),
    "natural-earth": ("earthlens.admin", "AdminBoundaries", "", {}),
    "tiger": ("earthlens.admin", "AdminBoundaries", "", {}),
    # ISRIC SoilGrids 2.0 — global 250 m soil properties (clay, sand,
    # silt, cfvo, phh2o, cec, nitrogen, soc, ocd, ocs, bdod) subset
    # server-side over OGC WCS at maps.isric.org and written as GeoTIFF
    # (OUTPUT_KIND="raster"; aggregate= rejected — static, no time axis).
    # No extra SDK — the WCS transport is pyramids' Dataset.from_wcs;
    # open, CC-BY 4.0, no auth. Alias "isric".
    "soilgrids": ("earthlens.soilgrids", "SoilGrids", "", {}),
    "isric": ("earthlens.soilgrids", "SoilGrids", "", {}),
    # Copernicus DEM (GLO-30 / GLO-90) over the anonymous AWS Open
    # Data buckets — the account-free path to a global DEM. Reuses
    # the [s3] unsigned-boto3 substrate; no new SDK. Aliases
    # "copernicus-dem" / "cop-dem" / "elevation".
    "dem": ("earthlens.dem", "DEM", "s3", {}),
    "copernicus-dem": ("earthlens.dem", "DEM", "s3", {}),
    "cop-dem": ("earthlens.dem", "DEM", "s3", {}),
    "elevation": ("earthlens.dem", "DEM", "s3", {}),
    # Drought-indicator backend over three live public services:
    # USDM (vector GeoJSON polygon classes), Copernicus EDO/GDO (raster
    # via the Copernicus drought GetCoverage REST endpoint —
    # TIME + SELECTED_TIMESCALE custom params, not a conformant WCS),
    # and CSIC SPEIbase (raster NetCDF). Per-instance OUTPUT_KIND from
    # the resolved `dataset=` row. No SDK extra (requests + pyramids are
    # core). The four keys (`drought` / `usdm` / `edo` / `gdo`) are
    # discoverability aliases — all four resolve to the same backend
    # and all four require an explicit `dataset=` kwarg (e.g.
    # `EarthLens("usdm", dataset="usdm", ...)`,
    # `EarthLens("edo", dataset="edo-spaST", ...)`). No alias pre-binds
    # the dataset: pre-bound aliases collide with the facade's own
    # `dataset=` plumbing (TypeError: multiple values) and only work for
    # exactly one of the catalog rows, so they trade a tiny ergonomic
    # win for two foot-guns.
    "drought": ("earthlens.drought", "Drought", "", {}),
    "usdm": ("earthlens.drought", "Drought", "", {}),
    "edo": ("earthlens.drought", "Drought", "", {}),
    "gdo": ("earthlens.drought", "Drought", "", {}),
}


def discover_backends() -> dict[str, BackendSpec]:
    """Merge the backend tables published by every installed provider package.

    Returns:
        A `key -> BackendSpec` mapping union of every entry point in the
        `earthlens.backends` group. Later entries win on a duplicate key.
    """
    merged: dict[str, BackendSpec] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        merged.update(ep.load())
    return merged
