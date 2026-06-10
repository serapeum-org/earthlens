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
