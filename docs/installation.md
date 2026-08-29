# Installation

## Stable Release

Please install earthlens in a virtual environment so that its requirements don't tamper with your system's Python.

### pip

The simplest way to install `earthlens` is from PyPI:

```bash
pip install earthlens
```

That pulls in every runtime dependency. Add the backend extras you need (see
[Available extras](#available-extras)) and you can skip the rest of these
instructions.

### conda

`earthlens` is also on the [conda-forge](https://conda-forge.org) channel:

```bash
conda install -c conda-forge earthlens
```

!!! warning "conda-forge can lag behind PyPI"
    The feedstock is updated separately from the PyPI release, so the conda
    channel is sometimes a release or two behind — which matters, because the
    public import moved to `earthlens.core` in 0.11.0 and 0.12.0 carried
    further breaking changes (see the [migration guide](migration.md)). Check
    what the channel actually offers before relying on it:

    ```bash
    conda search earthlens --channel conda-forge
    ```

    If it is behind, install from PyPI instead.

### uv

You can also use [uv](https://docs.astral.sh/uv/) to manage the environment:

```bash
uv add earthlens
```

### Installing Python

earthlens requires Python 3.11+.

For Python we recommend using the [Anaconda Distribution](https://www.anaconda.com/download/) for Python 3.

### Install as a conda environment

To keep earthlens isolated from other projects, create a dedicated conda
environment for it:

```bash
conda create -n earthlens python=3.12
```

Then activate it:

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
| `eedai` | [Google Earth Engine](https://serapeum-org.github.io/earthlens/reference/gee/usage/) — optional EEDAI fetch path | `pyramids-eo >=0.5.0,<0.6` |
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
| `openeo` | [openEO](https://serapeum-org.github.io/earthlens/reference/openeo/introduction/) | `openeo >=0.47,<0.52` |
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
| `all` | every backend above except `argo`, `osm-pbf` (see [What `earthlens[all]` excludes](#what-earthlensall-excludes-and-why)) | — |


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
pip install earthlens-imagery[gee]     # Earth Engine, without the other 53 backends
pip install earthlens-ocean[argo,cmems]
pip install earthlens-atmosphere[all]  # every atmosphere SDK
```

A thematic package depends only on `earthlens-core`, so its SDKs stay extras:
`pip install earthlens-imagery` gives you the imagery backends' code without
`earthengine-api`, `openeo`, `eumdac` and the rest.

**Why the split.** Two provider SDKs cannot coexist in one environment.
`argopy` (the `argo` extra) requires `xarray>=2025.7`, while `openeo` — which
*is* part of `earthlens[all]` — caps `xarray<2025.01.2`, and no single `xarray`
satisfies both. The root `pyproject.toml` declares those two extras conflicting
under `[tool.uv] conflicts`, so `uv` **forks** the lockfile: `argopy` and
`openeo` both live in one `uv.lock` on their own `xarray` (`2025.1.1` for the
`openeo` / `all` side, `2025.9.0` for the `argo` side), and each side installs
cleanly on its own. Splitting the backends across packages then lets you install
one domain's dependencies without inheriting every other domain's constraints.

### What `earthlens[all]` excludes, and why

`earthlens[all]` is the union of **every backend extra that can honestly share
one environment** — that is every extra in the table above **except three**:
`argo`, `osm-pbf` and `eedai`. Each is left out for a concrete reason:

| Excluded | SDK | Why it can't join `all` |
|---|---|---|
| `argo` | `argopy` | **Two** independent problems, either one disqualifying. **(1) `xarray` — a resolution conflict:** `argopy >=1.4` needs `xarray>=2025.7`, but `openeo` (in `all`) caps `xarray<2025.01.2` — disjoint ranges, which is what `[tool.uv] conflicts` declares. **(2) `erddapy` — a runtime break:** `argopy 1.4.0` still *resolves* (it does not cap `erddapy`) but fails at `import` — it imports `erddapy.erddapy._quote_string_constraints`, which `erddapy 3.3` removed — while the `erddap` extra (in `all`) requires `erddapy>=3.0`. |
| `osm-pbf` | `pyrosm` | `pyrosm` (0.11) and `osmium` (4.3.1) both ship wheels, but `pyrosm` pulls the **sdist-only** `cykhash` (no wheels for any Python), so adding `osm-pbf` to `all` would make `pip install earthlens[all]` require a C compiler — it is kept out to keep `all` wheel-only. Tracked in [#783](https://github.com/serapeum-org/earthlens/issues/783). |

| `eedai` | `pyramids-eo` | It resolves cleanly — this one is about *behaviour*, not packaging. Installing it activates the GEE backend's default `engine="auto"`, which serves raw single-asset reads through the EEDAI reader; that path samples and grids differently from Earth Engine (see the [GEE usage page](reference/gee/usage.md)). Holding it out of `all` means an upgrade never silently changes an existing user's pixels. |

(`osm` itself **is** in `all` — see the resolution note below for why.)

Each still installs **on its own**, in a separate environment:

```bash
pip install earthlens[argo]      # its own env — pulls xarray>=2025.7
pip install earthlens[osm-pbf]   # builds cykhash (a pyrosm dep) from source — needs a C compiler
```

> **What an `all` install actually resolves to.** With `argo` out,
> `earthlens[all]` lands on the `openeo` side of the fork, and `openeo 0.51`
> caps **both** `xarray<2025.01.2` **and** `pandas<3.0.0` — so an `all`
> environment runs on `xarray 2025.1.1` and `pandas 2.x`. That pandas-2.x floor
> is exactly what lets `osm` sit in `all`: `ohsome`'s own `pandas<3.0.0` agrees
> rather than collides. Every other backend works; asking the facade for an
> excluded one (e.g. `data_source="argo"`) without its extra raises a clear
> `ImportError` naming the extra to install.

> **Heads-up — `argo` is currently broken even on its own.** Since `erddapy 3.3`
> (released 2026-06-30) a plain `pip install earthlens[argo]` pulls `erddapy 3.3`,
> and `import argopy` then fails with
> `ImportError: cannot import name '_quote_string_constraints'`. This is an
> upstream `argopy` bug ([euroargodev/argopy#657](https://github.com/euroargodev/argopy/issues/657)),
> tracked for earthlens in
> [serapeum-org/earthlens#789](https://github.com/serapeum-org/earthlens/issues/789);
> the `argo` backend will work again once `argopy` ships a fix.

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

To verify the installation, import from `earthlens.core` and print the version:

```bash
python -c "from earthlens.core import __version__; print(__version__)"
```

A bare `import earthlens` is **not** a useful check — `earthlens` is a PEP 420
namespace package, so that import succeeds even when nothing is installed.

## Install using pip

Install earthlens with pip, picking the backend extras you
need (see [Available extras](#available-extras) for the full list):

```bash
pip install earthlens[ecmwf]
```

## Development install

If you are planning to contribute to earthlens, install the whole workspace
editable with the `[all]` extra so the full test suite (which exercises every
backend) can run. [uv](https://docs.astral.sh/uv/) is the supported path — it
installs every workspace member from one lockfile (`uv.lock`). `--extra all`
pulls every backend SDK except the two extras excluded from `all`
(`argo` / `osm-pbf` — see [What `earthlens[all]` excludes](#what-earthlensall-excludes-and-why)):

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
