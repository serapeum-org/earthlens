# Installation

## Stable Release

Please install earthlens in a virtual environment so that its requirements don't tamper with your system's Python.

### conda

The easiest way to install `earthlens` is using the `conda` package manager. `earthlens` is available in the [conda-forge](https://conda-forge.org) channel:

```bash
conda install -c conda-forge earthlens
```

If this works, it will install earthlens with all dependencies including Python and GDAL, and you can skip the rest of the installation instructions.

### pixi

You can also use [pixi](https://pixi.sh) to manage the environment:

```bash
pixi add earthlens
```

### Installing Python and GDAL dependencies

The main dependencies for earthlens are Python 3.11+ and GDAL.

For Python we recommend using the [Anaconda Distribution](https://www.anaconda.com/download/) for Python 3.

### Install as a conda environment

The easiest and most robust way to install earthlens is in a separate conda environment. In the root repository directory there is an `environment.yml` file that lists all dependencies:

```bash
conda env create -f environment.yml
```

This creates a new environment with the name `earthlens`. To activate it:

```bash
conda activate earthlens
```

Then install a release of earthlens from PyPI. Each backend's SDK
is an optional extra — pick the ones you actually need:

```bash
pip install earthlens[ecmwf]    # ECMWF / Copernicus CDS
pip install earthlens[gee]      # Google Earth Engine
pip install earthlens[cmems]    # Copernicus Marine
pip install earthlens[all]      # everything
```

### Available extras

Two tiers of extras exist. **Per-backend extras** pull in exactly one backend's SDK; **thematic
bundles** union several per-backend extras that cover the same kind of data (e.g. `earthlens[ocean]`
pulls `cmems` + `argo` + `erddap` together) — see [Data Sources](examples/data-sources.md) for how
they're grouped, or [_images/logos/ATTRIBUTION.md](https://github.com/serapeum-org/earthlens/blob/main/docs/_images/logos/ATTRIBUTION.md).
Both tiers are always available side by side — installing a bundle is equivalent to installing its
listed per-backend extras yourself.

#### Per-backend extras

| Extra | Provider | Pulls |
|---|---|---|
| `s3` | [AWS Open Data](https://serapeum-org.github.io/earthlens/reference/s3/introduction/) | `boto3 >=1.43.0`, `botocore >=1.34.0` |
| `ecmwf` | [Copernicus Climate Data Store (ECMWF)](https://serapeum-org.github.io/earthlens/reference/ecmwf/introduction/) | `cdsapi >=0.7.7` |
| `ecmwf-modern` | ECMWF (alternate SDK) | `ecmwf-datastores-client >=0.5.1` |
| `gee` | [Google Earth Engine](https://serapeum-org.github.io/earthlens/reference/gee/introduction/) | `earthengine-api >=1.7.26`, `google-api-python-client >=2.0`, `google-cloud-storage >=2.0`, `Rtree >=1.0.0`, `urllib3 >=1.26` |
| `cmems` | [Copernicus Marine Service](https://serapeum-org.github.io/earthlens/reference/cmems/introduction/) | `copernicusmarine >=2.0.0,<3` |
| `cmip6` | [WCRP CMIP6](https://serapeum-org.github.io/earthlens/reference/cmip6/introduction/) | *(none — no SDK needed)* |
| `fdsn` | [FDSN](https://serapeum-org.github.io/earthlens/reference/fdsn/introduction/) | `obspy >=1.5.0` |
| `nwm` | [NOAA National Water Model](https://serapeum-org.github.io/earthlens/reference/nwm/introduction/) | `earthlens[s3]`, `pyramids-gis[parquet] >=0.45.0` |
| `earthdata` | [NASA Earthdata](https://serapeum-org.github.io/earthlens/reference/earthdata/introduction/) | `earthaccess >=0.18.0; python_version >= '3.12'` |
| `asf` | [Alaska Satellite Facility (ASF)](https://serapeum-org.github.io/earthlens/reference/asf/introduction/) | `asf_search >=12.2.2`, `earthlens[earthdata]` |
| `eea_aq` | [European Environment Agency](https://serapeum-org.github.io/earthlens/reference/eea-aq/introduction/) | `airbase >=1.0`, `nest_asyncio >=1.5` |
| `hdx` | [Humanitarian Data Exchange (UN OCHA)](https://serapeum-org.github.io/earthlens/reference/hdx/introduction/) | `hdx-python-api >=6,<7` |
| `eumetsat` | [EUMETSAT](https://serapeum-org.github.io/earthlens/reference/eumetsat/introduction/) | `eumdac >=3.1` |
| `nwp` | [Herbie (NWP archive access)](https://serapeum-org.github.io/earthlens/reference/nwp/introduction/) | `herbie-data >=2026.3`, `ecmwf-opendata >=0.3`, `ecmwflibs; sys_platform == 'win32'` |
| `radar` | [NOAA NEXRAD](https://serapeum-org.github.io/earthlens/reference/radar/introduction/) | `earthlens[s3]` |
| `dem` | [Copernicus DEM (ESA)](https://serapeum-org.github.io/earthlens/reference/dem/introduction/) | `earthlens[s3]` |
| `goes` | [NOAA GOES-R](https://serapeum-org.github.io/earthlens/reference/goes/introduction/) | `earthlens[s3]` |
| `stac` | [STAC (SpatioTemporal Asset Catalog)](https://serapeum-org.github.io/earthlens/reference/stac/introduction/) | `pyramids-gis[stac] >=0.45.0` |
| `openeo` | [openEO](https://serapeum-org.github.io/earthlens/reference/openeo/introduction/) | `openeo >=0.47,<0.48` |
| `sentinel-hub` | [Sentinel Hub](https://serapeum-org.github.io/earthlens/reference/sentinel-hub/introduction/) | `sentinelhub >=3.11.5` |
| `tropycal` | [Tropycal](https://serapeum-org.github.io/earthlens/reference/tropycal/introduction/) | `tropycal >=1.4`, `cartopy >=0.22` |
| `overture` | [Overture Maps Foundation](https://serapeum-org.github.io/earthlens/reference/overture/introduction/) | `overturemaps >=1.0.0`, `duckdb >=1.0.0` |
| `usgs-water` | [USGS National Water Information System](https://serapeum-org.github.io/earthlens/reference/usgs-water/introduction/) | `dataretrieval >=1.1.4` |
| `ghsl` | [European Commission Joint Research Centre (GHSL)](https://serapeum-org.github.io/earthlens/reference/ghsl/introduction/) | *(none — no SDK needed)* |
| `worldpop` | [WorldPop](https://serapeum-org.github.io/earthlens/reference/worldpop/introduction/) | `worldpoppy >=0.4`, `py7zr >=0.20` |
| `gbif` | [GBIF](https://serapeum-org.github.io/earthlens/reference/gbif/introduction/) | `pygbif >=0.6.6` |
| `obis` | [OBIS](https://serapeum-org.github.io/earthlens/reference/obis/introduction/) | `pyobis >=1.6.1` |
| `jaxa` | [JAXA](https://serapeum-org.github.io/earthlens/reference/jaxa/introduction/) | `jaxa.earth >=0.1.6,<0.2`, `gportal >=0.4,<0.5` |
| `argo` | [Argo Program](https://serapeum-org.github.io/earthlens/reference/argo/introduction/) | `argopy >=1.4` |
| `erddap` | [NOAA ERDDAP](https://serapeum-org.github.io/earthlens/reference/erddap/introduction/) | `erddapy >=3.0` |
| `osm` | [OpenStreetMap](https://serapeum-org.github.io/earthlens/reference/osm/introduction/) | `overpy >=0.7`, `ohsome >=0.4.0` |
| `osm-pbf` | OpenStreetMap (bulk .osm.pbf extracts) | `pyrosm >=0.11.0`, `osmium >=4.3.1` |
| `all` | every backend above except `argo`, `osm`, `osm-pbf` (pin conflicts — see note below) | — |

#### Thematic bundles

| Extra | Covers (`data_source` keys) | Notes |
|---|---|---|
| `air-quality` | `airnow`, `eea-aq`, `openaq`, `sensor-community` |  |
| `biodiversity` | `gbif`, `iucn`, `obis`, `wdpa` |  |
| `climate` | `climate_indices`, `cmip6`, `ecmwf` |  |
| `disasters` | `fdsn`, `firms`, `gdacs`, `risk_indicators` |  |
| `elevation` | `bathymetry`, `dem` |  |
| `glaciers-cryosphere` | `glaciers` |  |
| `humanitarian` | `hdx` |  |
| `hydrology` | `nwm`, `usgs-water` |  |
| `ocean` | `argo`, `cmems`, `erddap` | Not meant to be combined with `platforms` in the same install — `argo`'s `argopy` needs `xarray>=2025.7`, `platforms`' `openeo` needs `xarray<2025.1.2`. |
| `platforms` | `earthdata`, `eumetsat`, `gee`, `goes`, `jaxa`, `openeo`, `s3`, `sentinel-hub`, `stac` |  |
| `population-settlement` | `ghsl`, `worldpop` |  |
| `precipitation-drought` | `chc`, `drought` |  |
| `renewable-energy` | `nrel`, `pvgis`, `solar_wind_atlas` |  |
| `sar-radar` | `asf` |  |
| `soil` | `soilgrids` |  |
| `tropical-cyclones` | `tropycal` |  |
| `vector-basemaps` | `admin`, `osm`, `overture` | Not meant to be combined with most other bundles — `osm`'s `ohsome` needs `pandas<3.0.0`, conflicting with the rest of the dependency graph. |
| `weather-forecast` | `nwp` |  |
| `weather-radar` | `radar` |  |

A bare `pip install earthlens` installs only the core dependencies (numpy, pandas, pyramids-gis,
requests, …), which is enough for every backend with no extra SDK of its own: **geoBoundaries**, **AirNow**, **GEBCO/ETOPO bathymetry**, **CHC/CHIRPS**, **climate teleconnection indices**, **USDM/EDO/GDO/SPEIbase**, **NASA FIRMS**, **GDACS**, **RGI/GLIMS/WGMS glaciers**, **IUCN Red List**, **NREL/National Laboratory of the Rockies**, **OpenAQ**, **PVGIS**, **ThinkHazard!/INFORM**, **Sensor.Community**, **SoilGrids**, **Global Solar/Wind Atlas**, **Protected Planet**.
Asking the facade for a backend whose extra is missing (e.g. `data_source="ecmwf"` without
`earthlens[ecmwf]`) raises a clear `ImportError` naming the extra to install.

> **Dependency note — `openeo` version pin.** openeo `0.48+` hard-caps
> `pandas<3.0.0`, which would drag the whole environment down to pandas 2.x.
> earthlens therefore pins `openeo >=0.47,<0.48` — the newest openeo that runs
> on **pandas 3** (validated live against CDSE) — so `earthlens[all]` and
> `earthlens[openeo]` keep pandas 3 for every backend. openeo `0.47` does still
> cap `xarray<2025.01.2`, so installing it constrains `xarray` (not `pandas`).
> The upper bound will be lifted once openeo ships a pandas-3-compatible release.

> **Dependency note — `argo`, `osm`, `osm-pbf` excluded from `all`.** `argopy` (via `argo`) pins
> `xarray>=2025.7`, which conflicts with `openeo`'s `xarray<2025.1.2`; `ohsome` (via `osm`) pins
> `pandas<3.0.0`, which conflicts with the rest of the locked dependency graph; `osm-pbf`'s `pyrosm`
> has no prebuilt wheel and must compile from source. All three still install fine on their own
> (`pip install earthlens[argo]`, `earthlens[osm]`, `earthlens[osm-pbf]`) — they're just excluded
> from the `all` union so it stays installable in one shot.

## From Sources

The sources for earthlens can be downloaded from the [GitHub repo](https://github.com/serapeum-org/earthlens).

Clone the public repository:

```bash
git clone https://github.com/serapeum-org/earthlens.git
```

Or download the tarball:

```bash
curl -OJL https://github.com/serapeum-org/earthlens/tarball/main
```

Once you have a copy of the source, you can install it with the
extras you need:

```bash
pip install -e ".[ecmwf]"
# or all backends at once:
pip install -e ".[all]"
```

To install directly from GitHub (from the HEAD of the main branch):

```bash
pip install "earthlens[ecmwf] @ git+https://github.com/serapeum-org/earthlens.git"
```

Or from a specific release:

```bash
pip install "earthlens[ecmwf] @ git+https://github.com/serapeum-org/earthlens.git@{release}"
```

Now you should be able to start Python and try `import earthlens` to verify the installation.

## Install using pip

Besides the recommended conda environment setup, you can also install earthlens with `pip`. For the more difficult to install Python dependencies, it is best to use conda:

```bash
conda install numpy scipy gdal pyproj
```

Then install earthlens with pip, picking the backend extras you
need (see "From PyPI" above for the available extras):

```bash
pip install earthlens[ecmwf]
```

## Development install

If you are planning to contribute to earthlens, do an editable install
with the `[all]` extra so the full test suite (which exercises every
backend) can run:

```bash
git clone https://github.com/serapeum-org/earthlens.git
cd earthlens
conda activate earthlens
pip install -e ".[all]"
```

More details on conda environments: [Managing environments](https://conda.io/docs/user-guide/tasks/manage-environments.html)
