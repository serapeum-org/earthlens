# Getting started

This page walks through your first download — from install to a file on disk —
using the unified `EarthLens` facade. For the full backend catalog see
[Supported providers](reference/providers.md); for runnable notebooks see the
**Examples** tab.

## 1. Install

```bash
pip install earthlens            # core + the no-SDK backends (CHC, GDACS, FIRMS, OpenAQ, GHSL)
pip install earthlens[ecmwf]     # add a backend that needs an SDK
pip install earthlens[all]       # everything
```

See [Installation](installation.md) for the complete extras table.

## 2. Pick a backend

Every provider is reached through the same `EarthLens` class by passing its key
as `data_source=`. The default is `"chc"` (CHIRPS). The full key list — with
aliases, auth, and output kind — is in [Supported providers](reference/providers.md).

A few backends need credentials before they will download (ECMWF needs
`~/.cdsapirc`, GEE a service account, CMEMS / Earthdata / EUMETSAT a login); the
no-SDK backends above work with no setup. See [Authentication
examples](examples/authentication.md).

## 3. Your first download

CHIRPS daily rainfall needs no credentials, so it makes the simplest first run:

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="chc",            # default; CHIRPS over anonymous FTP
    temporal_resolution="daily",
    start="2009-01-01",
    end="2009-01-03",
    variables=["precipitation"],
    lat_lim=[4.19, 4.64],          # [south, north]
    lon_lim=[-75.65, -74.73],      # [west, east]
    path="data/chirps",
)
lens.download()
```

This writes one cropped GeoTIFF per day under `data/chirps/`. Walk through the
same example with output and a plot in the [CHIRPS
quickstart](examples/chc/quickstart.ipynb).

## 4. What `download()` returns

`download()` returns whatever the backend produced, and the shape depends on the
backend's output kind:

- **Raster backends** (CHC, Amazon S3, ECMWF, GEE, GHSL, WorldPop, …) return the
  list of written file paths (`list[Path]`) — GeoTIFFs / NetCDFs, or export
  destinations for GEE's async exports.
- **Vector / tabular backends** (FDSN, GDACS, Overture, OpenAQ, USGS Water,
  Tropycal, …) return the assembled `GeoDataFrame` / `DataFrame`.

Either way the files land under the `path=` you supply. Read a raster result
back with [pyramids](https://github.com/serapeum-org/pyramids) (the GIS
backend), e.g. `pyramids.dataset.Dataset.read_file(...)`.

## 5. Common constructor arguments

| Argument | Meaning |
|----------|---------|
| `data_source` | Backend key (e.g. `"chc"`, `"ecmwf"`, `"gee"`). |
| `variables` | A list of variable names, or a `{dataset: [variable, …]}` mapping for catalog-driven backends (ECMWF, GEE, STAC, NWP). |
| `temporal_resolution` | Backend-dependent (`"daily"`, `"monthly"`, …). |
| `start` / `end` | Inclusive date range (`"%Y-%m-%d"` by default; override with `fmt=`). |
| `lat_lim` / `lon_lim` | Bounding box `[min, max]`; defaults to whole-Earth when omitted. |
| `path` | Output directory. |

Any extra keyword not named by the facade is forwarded verbatim to the backend
constructor — e.g. ECMWF's `skip_constraints=`, or GEE's `scale=` /
`export_via=` / `reducer=`. Credentials travel the same way for most backends
(CMEMS's `service_username=`, WDPA's `token=`), while a few take theirs in
`authenticate(...)` instead (GEE's `service_account=`, FIRMS's `api_key=`) —
see each backend's reference page.

## Next steps

- [Discovering datasets](discovery.md) — find which of the 61 providers has
  what you need, with `sources()` / `find()` / `search()`.
- [Temporal aggregation](aggregation.md) — reduce a downloaded stack into
  windowed composites (daily mean, monthly sum, …).
- [Architecture](overview/architecture.md) — how the facade, backends, and
  catalogs fit together.
- [Troubleshooting](troubleshooting.md) — when a download fails, and what to
  change.
- The **Examples** tab — a quickstart notebook for every backend.
