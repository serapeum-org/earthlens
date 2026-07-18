# Installation

## Stable Release

Please install earthlens in a virtual environment so that its requirements don't tamper with your system's Python.

### conda

The easiest way to install `earthlens` is using the `conda` package manager. `earthlens` is available in the [conda-forge](https://conda-forge.org) channel:

```bash
conda install -c conda-forge earthlens
```

If this works, it will install earthlens with all dependencies including Python and GDAL, and you can skip the rest of the installation instructions.

### uv

You can also use [uv](https://docs.astral.sh/uv/) to manage the environment:

```bash
uv add earthlens
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

Each extra pulls in exactly one backend's SDK. The names work the same whether
you install the meta-package or a thematic distribution directly:
`pip install earthlens[gee]` forwards to `earthlens-imagery[gee]`.

| Extra | Provider | Pulls |
|---|---|---|
| `s3` | [AWS Open Data](https://serapeum-org.github.io/earthlens/reference/s3/introduction/) | `boto3 >=1.43.0`, `botocore >=1.34.0` |
| `ecmwf` | [Copernicus Climate Data Store (ECMWF)](https://serapeum-org.github.io/earthlens/reference/ecmwf/introduction/) | `cdsapi >=0.7.7` |
| `ecmwf-modern` | ECMWF (alternate SDK) | `ecmwf-datastores-client >=0.5.1` |
| `gee` | [Google Earth Engine](https://serapeum-org.github.io/earthlens/reference/gee/introduction/) | `earthengine-api >=1.7.26`, `google-api-python-client >=2.0`, `google-cloud-storage >=2.0`, `Rtree >=1.0.0`, `urllib3 >=1.26` |
| `cmems` | [Copernicus Marine Service](https://serapeum-org.github.io/earthlens/reference/cmems/introduction/) | `copernicusmarine >=2.0.0,<3` |
| `cmip6` | [WCRP CMIP6](https://serapeum-org.github.io/earthlens/reference/cmip6/introduction/) | *(none — no SDK needed)* |
| `fdsn` | [FDSN](https://serapeum-org.github.io/earthlens/reference/fdsn/introduction/) | `obspy >=1.5.0` |
| `nwm` | [NOAA National Water Model](https://serapeum-org.github.io/earthlens/reference/nwm/introduction/) | `earthlens[s3]`, `pyramids-gis[parquet] >=0.46.0` |
| `earthdata` | [NASA Earthdata](https://serapeum-org.github.io/earthlens/reference/earthdata/introduction/) | `earthaccess >=0.18.0; python_version >= '3.12'` |
| `asf` | [Alaska Satellite Facility (ASF)](https://serapeum-org.github.io/earthlens/reference/asf/introduction/) | `asf_search >=12.2.2`, `earthlens[earthdata]` |
| `eea_aq` | [European Environment Agency](https://serapeum-org.github.io/earthlens/reference/eea-aq/introduction/) | `airbase >=1.0`, `nest_asyncio >=1.5` |
| `hdx` | [Humanitarian Data Exchange (UN OCHA)](https://serapeum-org.github.io/earthlens/reference/hdx/introduction/) | `hdx-python-api >=6,<7` |
| `eumetsat` | [EUMETSAT](https://serapeum-org.github.io/earthlens/reference/eumetsat/introduction/) | `eumdac >=3.1` |
| `nwp` | [Herbie (NWP archive access)](https://serapeum-org.github.io/earthlens/reference/nwp/introduction/) | `herbie-data >=2026.3`, `ecmwf-opendata >=0.3`, `ecmwflibs; sys_platform == 'win32'` |
| `radar` | [NOAA NEXRAD](https://serapeum-org.github.io/earthlens/reference/radar/introduction/) | `earthlens[s3]` |
| `dem` | [Copernicus DEM (ESA)](https://serapeum-org.github.io/earthlens/reference/dem/introduction/) | `earthlens[s3]` |
| `goes` | [NOAA GOES-R](https://serapeum-org.github.io/earthlens/reference/goes/introduction/) | `earthlens[s3]` |
| `stac` | [STAC (SpatioTemporal Asset Catalog)](https://serapeum-org.github.io/earthlens/reference/stac/introduction/) | `pyramids-gis[stac] >=0.46.0` |
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


A bare `pip install earthlens` installs only the core dependencies (numpy, pandas, pyramids-gis,
requests, …), which is enough for every backend with no extra SDK of its own: **geoBoundaries**, **AirNow**, **GEBCO/ETOPO bathymetry**, **CHC/CHIRPS**, **climate teleconnection indices**, **USDM/EDO/GDO/SPEIbase**, **NASA FIRMS**, **GDACS**, **RGI/GLIMS/WGMS glaciers**, **IUCN Red List**, **NREL/National Laboratory of the Rockies**, **OpenAQ**, **PVGIS**, **ThinkHazard!/INFORM**, **Sensor.Community**, **SoilGrids**, **Global Solar/Wind Atlas**, **Protected Planet**.
Asking the facade for a backend whose extra is missing (e.g. `data_source="ecmwf"` without
`earthlens[ecmwf]`) raises a clear `ImportError` naming the extra to install.

### Package layout

`earthlens` is a meta-package. Installing it pulls in `earthlens-core` plus
five thematic provider packages, each carrying a group of backends:

| Package | Covers | Backends |
|---------|--------|----------|
| `earthlens-core` | facade, abstractions, CLI — no provider SDKs | — |
| `earthlens-atmosphere` | weather · climate · air quality · solar/wind | `chc` `climate_indices` `cmip6` `drought` `ecmwf` `nwp` `amazon-s3` `airnow` `eea_aq` `openaq` `sensor_community` `goes` `radar` `tropycal` `nrel` `pvgis` `solar_wind_atlas` |
| `earthlens-ocean` | ocean · freshwater · marine life | `argo` `cmems` `erddap` `nwm` `usgs_water` `obis` |
| `earthlens-imagery` | satellite platforms · SAR · EO catalogs | `asf` `earthdata` `eumetsat` `gee` `jaxa` `openeo` `sentinel_hub` `stac` |
| `earthlens-land` | terrain · elevation · soil · ecology · population | `bathymetry` `dem` `ghsl` `glaciers` `gbif` `iucn` `soilgrids` `wdpa` `worldpop` |
| `earthlens-hazards` | hazards · humanitarian · vector basemaps | `fdsn` `firms` `gdacs` `risk_indicators` `admin` `osm` `overture` `hdx` |

This changes nothing about how you use earthlens: the import path is the same
(`from earthlens.core import EarthLens`, `earthlens.chc`, …), and every extra above
works exactly as before — `pip install earthlens[gee]` still installs Earth
Engine and nothing else.

**Installing a single domain.** If you only need one group, install that package
directly and skip the others' backends entirely:

```bash
pip install earthlens-imagery[gee]     # Earth Engine, without the other 40 backends
pip install earthlens-ocean[argo,cmems]
pip install earthlens-atmosphere[all]  # every atmosphere SDK
```

A thematic package depends only on `earthlens-core`, so its SDKs stay extras:
`pip install earthlens-imagery` gives you the imagery backends' code without
`earthengine-api`, `openeo`, `eumdac` and the rest.

**Why the split.** A few provider SDKs cannot coexist — `argopy` requires
`xarray >=2025.7` while `openeo <0.48` requires `xarray <2025.1.2`, and no
version satisfies both. Those extras are therefore excluded from `earthlens[all]`
(see the note below) and can be installed on their own or alongside their own
group. Splitting the backends across packages lets you install one domain's
dependencies without inheriting every other domain's constraints.

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

The repository is a workspace of seven distributions, so a source install has
to install the members too — `pip install -e .` on its own installs the
meta-package, which then looks for `earthlens-core` **on PyPI** at the version
in the working tree, and an unreleased version is not there:

```text
ERROR: No matching distribution found for earthlens-core==<version>
```

Install the whole workspace instead, naming every member on one command line so
pip resolves them locally rather than from the index:

```bash
pip install -e libs/core \
            -e libs/providers/atmosphere \
            -e libs/providers/ocean \
            -e libs/providers/imagery \
            -e libs/providers/land \
            -e libs/providers/hazards \
            -e ".[ecmwf]"
```

With [uv](https://docs.astral.sh/uv/) this is a single command — the members are
declared as a workspace in `pyproject.toml`, so one lock covers them all:

```bash
uv sync
```

If you only want one domain from a clone, its package plus core is enough:

```bash
pip install -e libs/core -e "libs/providers/imagery[gee]"
```

To install a **published release** directly from GitHub (its members are on PyPI,
so the meta-package resolves normally):

```bash
pip install "earthlens[ecmwf] @ git+https://github.com/serapeum-org/earthlens.git@{release}"
```

Installing the HEAD of `main` this way does **not** work: `main` carries an
unreleased version whose members are not on PyPI yet, and pip cannot install a
workspace's sibling packages out of a single git URL. Clone the repository and
use the editable workspace install above.

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

If you are planning to contribute to earthlens, install the whole workspace
editable with the `[all]` extra so the full test suite (which exercises every
backend) can run. [uv](https://docs.astral.sh/uv/) is the supported path — it
installs every workspace member from one lockfile (`uv.lock`). `--extra all`
pulls every backend SDK except the mutually exclusive `argo` / `osm` (which are
faked in their unit tests):

```bash
git clone https://github.com/serapeum-org/earthlens.git
cd earthlens
uv sync --extra all --group dev
uv run pytest -m "not e2e"
```

The equivalent with pip, which must name every member so they resolve from the
clone rather than from PyPI:

```bash
pip install -e libs/core \
            -e libs/providers/atmosphere \
            -e libs/providers/ocean \
            -e libs/providers/imagery \
            -e libs/providers/land \
            -e libs/providers/hazards \
            -e ".[all]"
```

The tests import earthlens from the **installed** distributions, not from the
source tree: `earthlens` is a PEP 420 namespace shared across all seven
distributions, and each provider's `earthlens.<backend>` subpackage lives in its
own source tree, reachable only through the finder an editable install sets up.
Running `pytest` against a clone without installing the workspace first will not
collect.

### Running the tests

Tests are co-located with the distribution they cover, under each member's
`tests/` directory:

```text
libs/core/tests/               # facade, base, CLI, grids, entry-point discovery
libs/providers/<theme>/tests/  # each theme's backend tests
                               # (atmosphere, ocean, imagery, land, hazards)
```

`uv run pytest -m "not e2e"` from the repo root runs the **whole** suite — the
root `pyproject.toml` lists all six member test roots in `testpaths`. To run (or
measure coverage for) a **single** distribution, point pytest at that member's
own config:

```bash
# one distribution, standalone
uv run pytest -c libs/providers/imagery/pyproject.toml libs/providers/imagery/tests -m "not e2e"
# or, equivalently, from inside the member
cd libs/providers/imagery && uv run pytest -m "not e2e"
```

CI mirrors this: `tests.yml` runs one lane per distribution
(`--cov=libs/<member>/src`, uploaded to Codecov under a per-member flag), so a
failure is attributed to the distribution that owns it. End-to-end tests
(`-m e2e`) are opt-in and run in the separate `tests-e2e.yml` workflow.

One coupling to note: `earthlens-core`'s entry-point discovery and facade tests
(`test_backends.py`, `test_earthlens.py`) exercise the **whole** registry — they
assert every provider's backends are discoverable — so they require all five
provider distributions to be installed, which the workspace dev setup
(`uv sync --extra all`) always provides. A consequence is that a cross-provider
entry-point or packaging regression surfaces in the `core` lane, not only the
offending provider's lane.

More details on conda environments: [Managing environments](https://conda.io/docs/user-guide/tasks/manage-environments.html)
