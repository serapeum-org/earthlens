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

| Extra | Backend(s) | Pulls |
|-------|------------|-------|
| `s3` | Amazon S3 (AWS Open Data) | `boto3`, `botocore` |
| `ecmwf` | ECMWF / Copernicus CDS | `cdsapi` |
| `gee` | Google Earth Engine | `earthengine-api`, `google-api-python-client`, `google-cloud-storage`, `Rtree` |
| `cmems` | Copernicus Marine | `copernicusmarine` |
| `earthdata` | NASA Earthdata | `earthaccess` (Python ≥ 3.12) |
| `eumetsat` | EUMETSAT Data Store | `eumdac` |
| `fdsn` | FDSN seismic events | `obspy` |
| `stac` | STAC (MPC / Earth Search / CDSE) | `pyramids-gis[stac]` |
| `openeo` | openEO server-side processing | `openeo` |
| `sentinel-hub` | Sentinel Hub render | `sentinelhub` |
| `nwp` | Open NWP forecasts | `herbie-data`, `ecmwf-opendata` |
| `radar` | NEXRAD radar | `boto3`, `botocore` |
| `hdx` | Humanitarian Data Exchange | `hdx-python-api` |
| `overture` | Overture Maps | `overturemaps`, `duckdb` |
| `tropycal` | Tropical cyclones | `tropycal`, `cartopy` |
| `usgs-water` | USGS Water (NWIS) | `dataretrieval` |
| `worldpop` | WorldPop | `worldpoppy`, `py7zr` |
| `all` | every backend above | all of the above |

A bare `pip install earthlens` installs only the core dependencies
(numpy, pandas, pyramids-gis, requests, …), which is enough for the
backends that need no extra SDK: **CHC / CHIRPS** (anonymous FTP),
**GDACS**, **FIRMS**, **OpenAQ**, and **GHSL** (plain HTTPS + the core
GIS stack). Asking the facade for a backend whose extra is missing
(e.g. `data_source="ecmwf"` without `earthlens[ecmwf]`) raises a clear
`ImportError` naming the extra to install.

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
(`from earthlens import EarthLens`, `earthlens.chc`, …), and every extra above
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

With [pixi](https://pixi.sh) this is a single command — the members are declared
as editable path dependencies in `pyproject.toml`:

```bash
pixi install
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
backend) can run. [pixi](https://pixi.sh) is the supported path — it installs
every member from `pyproject.toml`'s editable path dependencies and resolves
them from one lockfile:

```bash
git clone https://github.com/serapeum-org/earthlens.git
cd earthlens
pixi install
pixi run test-no-e2e
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
source tree: `earthlens` is a regular package owned by `earthlens-core`, so a
provider living in a different source tree is only reachable through the finder
an editable install sets up. Running `pytest` against a clone without installing
the workspace first will not collect.

More details on conda environments: [Managing environments](https://conda.io/docs/user-guide/tasks/manage-environments.html)
