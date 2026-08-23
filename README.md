<p align="center">
  <img src="docs/_images/branding/earthlens-brand-kit/docs/readme-banner.png" width="820"
       alt="earthlens — 61 Earth-observation providers · one facade · modular install">
</p>

[![Tests](https://github.com/serapeum-org/earthlens/actions/workflows/tests.yml/badge.svg)](https://github.com/serapeum-org/earthlens/actions/workflows/tests.yml)
[![Wheel](https://github.com/serapeum-org/earthlens/actions/workflows/wheel-test.yml/badge.svg)](https://github.com/serapeum-org/earthlens/actions/workflows/wheel-test.yml)
[![Docs](https://github.com/serapeum-org/earthlens/actions/workflows/github-pages-mkdocs.yml/badge.svg)](https://serapeum-org.github.io/earthlens/)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/earthlens)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![conda-forge feedstock](https://img.shields.io/badge/conda--forge-feedstock-blue?logo=condaforge&logoColor=white)](https://github.com/conda-forge/earthlens-feedstock)


[![codecov](https://codecov.io/gh/serapeum-org/earthlens/branch/main/graph/badge.svg)](https://codecov.io/gh/serapeum-org/earthlens)
![GitHub last commit](https://img.shields.io/github/last-commit/serapeum-org/earthlens)
![GitHub forks](https://img.shields.io/github/forks/serapeum-org/earthlens?style=social)
![GitHub Repo stars](https://img.shields.io/github/stars/serapeum-org/earthlens?style=social)


Current release info
====================

| Name                                                                                                               | Downloads                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Version                                                                                                                                                                                                                                                                                                                                           | Platforms                                                                                                               |
|--------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------|
| [![Conda Recipe](https://img.shields.io/badge/recipe-earthlens-green.svg)](https://anaconda.org/conda-forge/earthlens) | [![Conda Downloads](https://img.shields.io/conda/dn/conda-forge/earthlens.svg)](https://anaconda.org/conda-forge/earthlens) [![Downloads](https://pepy.tech/badge/earthlens)](https://pepy.tech/project/earthlens) [![Downloads](https://pepy.tech/badge/earthlens/month)](https://pepy.tech/project/earthlens) [![Downloads](https://pepy.tech/badge/earthlens/week)](https://pepy.tech/project/earthlens) ![PyPI - Downloads](https://img.shields.io/pypi/dd/earthlens?color=blue&style=flat-square) ![GitHub all releases](https://img.shields.io/github/downloads/serapeum-org/earthlens/total) | [![Conda Version](https://img.shields.io/conda/vn/conda-forge/earthlens.svg)](https://anaconda.org/conda-forge/earthlens) [![PyPI version](https://badge.fury.io/py/earthlens.svg)](https://badge.fury.io/py/earthlens) [![Anaconda-Server Badge](https://anaconda.org/conda-forge/earthlens/badges/version.svg)](https://anaconda.org/conda-forge/earthlens) | [![Conda Platforms](https://img.shields.io/conda/pn/conda-forge/earthlens.svg)](https://anaconda.org/conda-forge/earthlens) |

earthlens — a unified Python client for satellite & climate data
=====================================================================

<p align="center">
  <img src="docs/_images/animation/earthlens-satellites-bluemarble.webp" width="820"
       alt="The satellite fleet earthlens can reach, in orbit — ground swaths sweeping across Earth">
</p>

<p align="center">
  <sub>34 of the spacecraft behind earthlens' providers, on their published orbits. The trailing band under
  each one is its instrument's real ground swath, and the trapezoid above it is the sensor footprint sweeping
  that strip out.<br>
  The clock counts <b>simulated orbital time at 270&times; real</b>, so 20 seconds of clip is 1.5 hours in
  orbit. Earth turns 22.6&deg; in that window and a low orbiter gets about nine tenths of the way round —
  which is how you read its ~90-minute period straight off the screen. The geostationary satellites look
  frozen because they are keeping pace with the ground beneath them.</sub>
</p>


**earthlens** gives you one consistent Python API for downloading satellite,
climate, and geospatial data from **61 providers** — climate reanalysis,
satellite imagery, ocean models, weather forecasts, natural-hazard feeds, air
quality, biodiversity, population, and more — and turning the results into
analysis-ready GeoTIFFs, GeoDataFrames, or tables.

It is part of the [serapeum-org](https://github.com/serapeum-org)
open-source ecosystem and is built on top of
[`pyramids-gis`](https://github.com/serapeum-org/pyramids) for raster I/O.


Why earthlens?
------------

<p align="center">
  <img src="docs/_images/branding/earthlens-brand-kit/animation/earthlens-logo-orbit.gif" width="600"
       alt="A satellite orbiting the earthlens globe">
</p>

Every provider speaks its own dialect: CHIRPS is anonymous FTP with date-coded
filenames, ERA5-on-S3 is unsigned object storage with a per-month layout, the
ECMWF CDS expects a JSON request body validated against a constraints graph,
Google Earth Engine is a server-side image-collection model, and the other 44
each have their own. **earthlens** flattens all of it into one call:

```python
from earthlens.core import EarthLens

earthlens = EarthLens(
    data_source="ecmwf",          # or "chc" (alias "chirps"), "amazon-s3", "gee"
    temporal_resolution="monthly",
    start="2022-01-01",
    end="2022-12-01",
    variables={
        "reanalysis-era5-single-levels-monthly-means": [
            "2m-temperature",
            "total-precipitation",
        ],
    },
    lat_lim=[37.0, 38.0],
    lon_lim=[23.0, 24.0],
    path="data/era5",
)
earthlens.download()
```

You get back per-date, per-variable GeoTIFFs in `data/era5/` — clipped to your
bbox, ready to feed into a hydrology model, a PV-yield notebook, a heat-wave
study, or anything else downstream.


Features
--------

- **61 backends, one facade.** `EarthLens(data_source=...)` routes to any
  provider without changing the rest of your code. Backends are discovered
  through entry points and imported lazily, so the SDK for a provider you never
  touch is never loaded.
- **Cross-provider discovery.** `find("precipitation")` tells you which of the
  61 providers serve a dataset, offline, before you commit to one; `search(...)`
  dry-runs a request and lists exactly what it would fetch.
- **YAML variable catalogs** per provider — every variable carries metadata:
  NetCDF name, units, accumulation semantics (`is_flux`), allowed pressure
  levels, monthly counterparts. Browseable with `Catalog().get_variable(...)`.
- **Pre-flight request validation** against the live CDS `constraints.json`
  graph. Bad date / area / variable combinations are rejected before bytes
  go over the wire, with actionable error messages.
- **Temporal aggregation built in.** Pass an `AggregationConfig` to
  `download()` and earthlens emits aggregated GeoTIFFs alongside the raw
  NetCDFs. `op="auto"` reduces **state** variables (temperature, SST, soil
  moisture) by mean and **flux** variables (precipitation, radiation,
  evaporation) by sum — the physically correct choice driven by catalog
  metadata.
- **Pressure-level support.** ERA5 pressure-level fields (4-D NetCDFs) can be
  sliced to a specific level on download.
- **Bbox cropping & NetCDF→GeoTIFF conversion** are handled by `pyramids-gis`
  under the hood.
- **Modular install extras** — only install the SDK for the backend you need
  (`pip install earthlens[ecmwf]`, `[s3]`, `[gee]`).
- **Bounded by design.** Streamed, atomic downloads; pooled HTTP connections
  with `Retry-After`-aware back-off; an exact `limit=` cap for vector/tabular
  requests; re-runs skip artefacts that are already complete.
- **Strictly typed.** Pydantic v2 models for catalog rows and request specs;
  modern PEP 585/604 type hints; Python 3.11 – 3.14 tested in CI.


Supported data sources
----------------------

`earthlens` wraps 61 providers behind the one `EarthLens(data_source=..., ...)` facade —
pass the `data_source` value below and everything else (auth, request shaping, output
format) is handled per-backend. See [Data Sources](https://serapeum-org.github.io/earthlens/examples/data-sources/)
for the full walkthrough of each one.

**Air quality**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/airnow.svg" height="20"> | [AirNow (US EPA)](https://www.airnow.gov/) | `airnow` |
| <img src="docs/_images/logos/eea-aq.svg" height="20"> | [European Environment Agency](https://www.eea.europa.eu/) | `eea-aq` |
| <img src="docs/_images/logos/openaq.svg" height="20"> | [OpenAQ](https://openaq.org/) | `openaq` |
| <img src="docs/_images/logos/sensor-community.png" height="20"> | [Sensor.Community](https://sensor.community/) | `sensor-community` |

**Biodiversity & protected areas**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/gbif.svg" height="20"> | [GBIF](https://www.gbif.org/) | `gbif` |
| <img src="docs/_images/logos/iucn.svg" height="20"> | [IUCN Red List](https://www.iucnredlist.org/) | `iucn` |
| <img src="docs/_images/logos/obis.png" height="20"> | [OBIS](https://obis.org/) | `obis` |
| <img src="docs/_images/logos/wdpa.png" height="20"> | [Protected Planet (UNEP-WCMC)](https://www.protectedplanet.net/) | `wdpa` |

**Climate reanalysis & projections**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/ecmwf.png" height="20"> | [Copernicus Climate Data Store (ECMWF)](https://cds.climate.copernicus.eu) | `ecmwf` |
| <img src="docs/_images/logos/climate_indices.svg" height="20"> | [NOAA Physical Sciences Laboratory](https://psl.noaa.gov/data/climateindices/) | `climate-indices` |
| <img src="docs/_images/logos/cmip6.svg" height="20"> | [WCRP CMIP6](https://wcrp-cmip.org/) | `cmip6` |

**Disasters & risk**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/fdsn.png" height="20"> | [FDSN](https://www.fdsn.org/) | `fdsn` |
| <img src="docs/_images/logos/gdacs.png" height="20"> | [GDACS](https://www.gdacs.org/) | `gdacs` |
| <img src="docs/_images/logos/firms.png" height="20"> | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) | `firms` |
| <img src="docs/_images/logos/risk_indicators.svg" height="20"> | [ThinkHazard! (GFDRR/World Bank)](https://thinkhazard.org) | `risk-indicators` |

**Elevation & bathymetry**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/dem.svg" height="20"> | [Copernicus DEM (ESA)](https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model) | `dem` |
| <img src="docs/_images/logos/bathymetry.png" height="20"> | [GEBCO](https://www.gebco.net/) | `bathymetry` |

**Glaciers & cryosphere**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/glaciers.png" height="20"> | [NSIDC Randolph Glacier Inventory](https://nsidc.org/data/nsidc-0770/versions/7) | `glaciers` |

**Humanitarian data**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/hdx.png" height="20"> | [Humanitarian Data Exchange (UN OCHA)](https://data.humdata.org) | `hdx` |

**Hydrology**

| | Provider | `data_source` |
|---|---|---|
|   | [NOAA National Water Model](https://water.noaa.gov/about/nwm) | `nwm` |
| <img src="docs/_images/logos/usgs-water.svg" height="20"> | [USGS National Water Information System](https://waterdata.usgs.gov/) | `usgs-water` |

**Multi-mission imagery & data platforms**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/s3.png" height="20"> | [AWS Open Data](https://registry.opendata.aws/) | `amazon-s3` |
| <img src="docs/_images/logos/eumetsat.svg" height="20"> | [EUMETSAT](https://www.eumetsat.int/) | `eumetsat` |
| <img src="docs/_images/logos/gee.png" height="20"> | [Google Earth Engine](https://earthengine.google.com/) | `gee` |
| <img src="docs/_images/logos/jaxa.svg" height="20"> | [JAXA](https://www.jaxa.jp/) | `jaxa` |
| <img src="docs/_images/logos/earthdata.png" height="20"> | [NASA Earthdata](https://www.earthdata.nasa.gov/) | `earthdata` |
| <img src="docs/_images/logos/goes.png" height="20"> | [NOAA GOES-R](https://www.goes-r.gov/) | `goes` |
| <img src="docs/_images/logos/stac.png" height="20"> | [STAC (SpatioTemporal Asset Catalog)](https://stacspec.org/) | `stac` |
| <img src="docs/_images/logos/sentinel-hub.png" height="20"> | [Sentinel Hub](https://www.sentinel-hub.com/) | `sentinel-hub` |
| <img src="docs/_images/logos/openeo.png" height="20"> | [openEO](https://openeo.org) | `openeo` |

**Ocean**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/argo.png" height="20"> | [Argo Program](https://argo.ucsd.edu/) | `argo` |
| <img src="docs/_images/logos/cmems.svg" height="20"> | [Copernicus Marine Service](https://marine.copernicus.eu/) | `cmems` |
| <img src="docs/_images/logos/erddap.svg" height="20"> | [NOAA ERDDAP](https://www.ncei.noaa.gov/erddap/information.html) | `erddap` |

**Population & human settlement**

| | Provider | `data_source` |
|---|---|---|
|   | [European Commission Joint Research Centre (GHSL)](https://ghsl.jrc.ec.europa.eu/) | `ghsl` |
| <img src="docs/_images/logos/worldpop.png" height="20"> | [WorldPop](https://hub.worldpop.org) | `worldpop` |

**Precipitation & drought**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/chc.png" height="20"> | [Climate Hazards Center (UCSB)](https://www.chc.ucsb.edu/) | `chc` |
| <img src="docs/_images/logos/drought.svg" height="20"> | [Copernicus European Drought Observatory / NDMC](https://drought.emergency.copernicus.eu/) | `drought` |

**Renewable energy**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/solar_wind_atlas.svg" height="20"> | [Global Solar Atlas / Global Wind Atlas (World Bank/ESMAP)](https://globalsolaratlas.info/) | `solar-wind-atlas` |
| <img src="docs/_images/logos/nrel.svg" height="20"> | [National Laboratory of the Rockies (formerly NREL)](https://www.nlr.gov/) | `nrel` |
| <img src="docs/_images/logos/pvgis.svg" height="20"> | [PVGIS (EU JRC)](https://re.jrc.ec.europa.eu/pvg_tools/) | `pvgis` |

**SAR / radar imagery**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/asf.png" height="20"> | [Alaska Satellite Facility (ASF)](https://asf.alaska.edu/) | `asf` |

**Soil**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/soilgrids.svg" height="20"> | [ISRIC SoilGrids](https://www.isric.org/explore/soilgrids) | `soilgrids` |

**Tropical cyclones**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/tropycal.png" height="20"> | [Tropycal](https://tropycal.github.io/tropycal/) | `tropycal` |

**Vector basemaps & boundaries**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/osm.svg" height="20"> | [OpenStreetMap](https://www.openstreetmap.org/) | `osm` |
| <img src="docs/_images/logos/overture.svg" height="20"> | [Overture Maps Foundation](https://overturemaps.org/) | `overture` |
|   | [geoBoundaries](https://www.geoboundaries.org/) | `admin` |

**Weather forecast (NWP)**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/nwp.png" height="20"> | [Herbie (NWP archive access)](https://herbie.readthedocs.io) | `nwp` |

**Weather radar**

| | Provider | `data_source` |
|---|---|---|
| <img src="docs/_images/logos/radar.svg" height="20"> | [NOAA NEXRAD](https://www.roc.noaa.gov/) | `radar` |

Logos are each provider's own mark, used only to identify which service a backend talks to
(not an endorsement of earthlens by that provider) — see
[docs/_images/logos/ATTRIBUTION.md](docs/_images/logos/ATTRIBUTION.md) for sourcing and rights
notes on every logo, including the handful of providers with no distinct mark of their own.


Installation
------------

`earthlens` is published on conda-forge and PyPI.

```bash
# pip — latest release
pip install earthlens

# conda
conda install -c conda-forge earthlens

# pip — bleeding edge
pip install git+https://github.com/serapeum-org/earthlens
```

To list all available versions on your platform:

```bash
conda search earthlens --channel conda-forge
```

A plain `pip install` is enough — earthlens pulls in everything it needs.

Backend SDKs are optional and pulled in by extras:

```bash
pip install earthlens[ecmwf]   # cdsapi
pip install earthlens[s3]      # boto3 + botocore
pip install earthlens[gee]     # earthengine-api
pip install earthlens[all]     # every backend SDK
```

`[all]` deliberately omits exactly two extras — `argo` and `osm-pbf`. `argopy` requires
`xarray>=2025.7` while `openeo` (which *is* in `all`) caps `xarray<2025.1.2`, and `pyrosm`
pulls the sdist-only `cykhash`, which would make `[all]` need a C compiler. `osm` itself
**is** included. Both excluded extras install fine on their own — see
[what `[all]` excludes](https://serapeum-org.github.io/earthlens/installation/#what-earthlensall-excludes-and-why).

For a development environment the repo is a [uv](https://docs.astral.sh/uv/)
workspace — `dev` and `docs` are dependency **groups**, not extras:

```bash
uv sync --extra all --group dev
```

See [Contributing](https://serapeum-org.github.io/earthlens/contributing/) for
the full setup.


Quick examples per backend
--------------------------

**CHIRPS daily rainfall** — anonymous FTP, no credentials.

```python
from earthlens.core import EarthLens

EarthLens(
    data_source="chc",
    temporal_resolution="daily",
    start="2009-01-01",
    end="2009-01-10",
    variables=["precipitation"],
    lat_lim=[4.19, 4.64],
    lon_lim=[-75.65, -74.73],
    path="data/chirps",
).download(cores=4)  # parallel FTP fetch
```

**ERA5 monthly via AWS public S3** — unsigned, fast, no API key.

```python
EarthLens(
    data_source="amazon-s3",
    temporal_resolution="monthly",
    start="2020-01-01",
    end="2020-12-01",
    variables=["air_temperature_at_2_metres", "precipitation_amount_1hour_Accumulation"],
    lat_lim=[30.0, 35.0],
    lon_lim=[28.0, 35.0],
    path="data/era5-s3",
).download()
```

**ECMWF CDS with on-the-fly aggregation.** Downloads daily ERA5, then writes
monthly GeoTIFFs aggregated with the right reduction per variable (mean for
temperature, sum for precipitation):

```python
from earthlens.core import EarthLens, AggregationConfig

EarthLens(
    data_source="ecmwf",
    temporal_resolution="daily",
    start="2022-06-01",
    end="2022-08-31",
    variables={
        "reanalysis-era5-single-levels": [
            "2m-temperature",
            "total-precipitation",
        ],
    },
    lat_lim=[37.0, 38.0],
    lon_lim=[23.0, 24.0],
    path="data/athens-summer",
).download(aggregate=AggregationConfig(freq="1MS", op="auto"))
```

**Google Earth Engine** — server-side collection, downloaded as GeoTIFFs. The
request is `{asset_id: [band, ...]}`, and GEE needs a service account:

```python
EarthLens(
    data_source="gee",
    temporal_resolution="monthly",       # one composite image per month
    start="2020-06-01",
    end="2020-08-31",
    variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
    lat_lim=[28.0, 32.0],
    lon_lim=[30.0, 34.0],
    path="data/gee",
    scale=5566,                          # output pixel size in metres
).authenticate(
    service_account="my-sa@my-project.iam.gserviceaccount.com",
    service_key="/path/to/key.json",
).download()
```


Aggregation: state vs flux
--------------------------

ERA5 mixes two physically distinct kinds of variables:

- **State** variables are instantaneous samples — temperature, SST, soil
  moisture, snow depth. Aggregating in time means **averaging**.
- **Flux** variables are accumulated over each timestep — precipitation,
  radiation, evaporation, surface heat fluxes. Aggregating in time means
  **summing**.

Mixing those up produces silently wrong results (a "monthly mean" of
precipitation under-reports rainfall by ~30×). earthlens's catalog tags every
variable with `is_flux`, and `op="auto"` reads that flag to pick the right
reduction:

```python
from earthlens.ecmwf import Catalog
spec = Catalog().get_variable(
    "reanalysis-era5-single-levels", "total-precipitation"
)
print(spec.is_flux)  # True  -> auto-aggregate by SUM
```

You can override with `op="mean" | "sum" | "max" | "min"` when you know
better than the catalog.


Authentication
--------------

Roughly half the backends need no credentials at all. Common ones:

| Source | What you need |
|---|---|
| CHIRPS / CHC | Nothing — anonymous FTP. |
| Amazon S3, Copernicus DEM, GOES, NWM, NEXRAD | Nothing — unsigned, public buckets. |
| GDACS, GHSL, Overture, HDX, SoilGrids, PVGIS, admin | Nothing — public HTTP. |
| ECMWF / CDS | A free CDS account and a `~/.cdsapirc` with your API key. |
| GEE | A Google Earth Engine project and a service-account JSON key. |
| CMEMS, Earthdata, ASF, EUMETSAT, Sentinel Hub, openEO | A provider login. |
| OpenAQ, AirNow, FIRMS, WDPA, IUCN, NREL, GFW | A free API key or token. |

Where credentials go is **backend-specific**: some take them as constructor keywords (CMEMS's
`service_username=` / `service_password=`), others in `authenticate(...)` (GEE's `service_account=`, FIRMS's
`api_key=`), and most fall back to an environment variable. Each backend's page says which. The full
per-provider matrix is in
[Supported providers](https://serapeum-org.github.io/earthlens/reference/providers/).


Documentation
-------------

Full docs, API reference, architecture diagrams, and a gallery of domain-specific
example notebooks (hydrology, oceanography, agriculture, solar/wind energy,
heat waves, drought, snow & cryosphere, climate-change anomalies) live at:

> **<https://serapeum-org.github.io/earthlens/>**

Start here:

| Page | What it covers |
|---|---|
| [Getting started](https://serapeum-org.github.io/earthlens/getting-started/) | Install to first file on disk. |
| [Discovering datasets](https://serapeum-org.github.io/earthlens/discovery/) | `sources()` / `find()` / `search()` across all 61 providers. |
| [Supported providers](https://serapeum-org.github.io/earthlens/reference/providers/) | Keys, output kinds, auth, and extras for every backend. |
| [Temporal aggregation](https://serapeum-org.github.io/earthlens/aggregation/) | Reduce a stack into windowed composites. |
| [Troubleshooting](https://serapeum-org.github.io/earthlens/troubleshooting/) | When a download fails, and what to change. |
| [Migration guide](https://serapeum-org.github.io/earthlens/migration/) | Breaking changes by release. |
| [Architecture](https://serapeum-org.github.io/earthlens/overview/architecture/) | How the facade, registry, and backends fit together. |


Contributing
------------

Issues, PRs, and discussions are welcome on
[GitHub](https://github.com/serapeum-org/earthlens). The repo uses pre-commit
with **ruff** (`ruff-check` + `ruff-format`), mypy, and bandit — install the
hooks once with `pre-commit install`.

See the [contributing guide](https://serapeum-org.github.io/earthlens/contributing/)
for the workspace layout, how to run the tests, and how to add a new provider
backend.


License
-------

GPL v3. See [LICENSE](LICENSE).
