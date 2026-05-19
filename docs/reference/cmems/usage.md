# Using the Copernicus Marine backend

This page is the hands-on guide to the `earthlens` CMEMS backend —
picking a dataset, building a download, and the trade-offs around
depth, output format, and the curated catalog. For background see the
[Introduction](introduction.md); for credentials see
[Authentication](authentication.md); the rendered API is on the
[Reference](cmems.md) page.

> **Install:** the backend needs the Copernicus Marine SDK —
> `pip install earthlens[cmems]` (which adds `copernicusmarine`). The
> `EarthLens` facade imports without it; `import earthlens.cmems`
> requires it.

## 1. Find a dataset and its variables

Curated rows live under `earthlens.cmems.Catalog`. Each entry binds a
CMEMS `dataset_id` to its variable short names, units, CF long-names,
cadence, domain, and temporal coverage:

```python
from earthlens.cmems import Catalog

cat = Catalog()
"cmems_mod_glo_phy_my_0.083deg_P1D-m" in cat.datasets   # True
ds = cat.get_dataset("cmems_mod_glo_phy_my_0.083deg_P1D-m")
ds.cadence       # 'daily'
ds.domain        # 'global'
sorted(ds.variables)
# ['so', 'thetao', 'uo', 'vo', 'zos']
cat.get_variable("cmems_mod_glo_phy_my_0.083deg_P1D-m", "thetao").units
# 'degrees_C'
```

`available_products:` is the live informational index of every CMEMS
product id the toolbox publishes (regenerate with
`tools/cmems/refresh_cmems_catalog.py refresh`); `datasets:` is the
curated subset earthlens models in detail. **Uncurated dataset ids
still work** — the catalog lookup is a metadata convenience, not a
gate. Pass any id the toolbox's `copernicusmarine.describe()`
recognises and the backend issues the request unchanged.

## 2. Download

```python
from earthlens.cmems import CMEMS

cmems = CMEMS(
    start="2020-01-01",
    end="2020-01-07",
    temporal_resolution="daily",
    variables={
        "cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao", "so", "zos"],
    },
    lat_lim=[30.0, 36.0],          # [lat_min, lat_max]
    lon_lim=[-10.0, -4.0],         # [lon_min, lon_max]
    path="data/cmems",
    service_username="YOUR_CMEMS_USERNAME",
    service_password="YOUR_CMEMS_PASSWORD",
    minimum_depth=0.0,
    maximum_depth=200.0,
)
paths = cmems.download()
# -> [PosixPath('data/cmems/cmems_mod_glo_phy_my_0_083deg_P1D-m.nc')]
```

The request is `{dataset_id: [variable, ...]}`. One `subset()` call
runs per `(dataset_id, variables)` pair; the toolbox returns a single
NetCDF per pair covering the full requested space/time/depth window.
`download()` returns the absolute paths the toolbox actually wrote.

The same call works through the unified facade:

```python
from earthlens import EarthLens

earthlens = EarthLens(
    data_source="cmems",
    start="2020-01-01",
    end="2020-01-07",
    variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao", "so"]},
    lat_lim=[30.0, 36.0],
    lon_lim=[-10.0, -4.0],
    path="data/cmems",
    service_username="YOUR_CMEMS_USERNAME",
    service_password="YOUR_CMEMS_PASSWORD",
)
earthlens.download()
```

The facade forwards every extra kwarg (`service_username`,
`service_password`, `credentials_file`, `file_format`, `minimum_depth`,
`maximum_depth`, `overwrite`) to the backend constructor unchanged.

## 3. Credentials

`service_username` + `service_password` use a free Copernicus Marine
portal account (register at <https://marine.copernicus.eu/register>).
Three other credential sources are accepted; see
[Authentication](authentication.md) for the resolution order. In
short:

- explicit `service_username=` / `service_password=` to `CMEMS(...)`,
- `COPERNICUSMARINE_SERVICE_USERNAME` / `COPERNICUSMARINE_SERVICE_PASSWORD`
  environment variables (toolbox-native),
- a saved `~/.copernicusmarine/.copernicusmarine-credentials` from a
  previous `copernicusmarine login`,
- an explicit `credentials_file=` path (CI-friendly).

If none of those resolve, `CMEMS(...)` raises `AuthenticationError`
on first download attempt rather than blocking on the toolbox's
interactive prompt.

## 4. Depth axis

CMEMS physics and biogeochemistry datasets are 4-D — the toolbox
returns a `(time, depth, lat, lon)` NetCDF for variables like `thetao`
/ `so` / `chl`. The CMEMS surface-only datasets (OSTIA SST, altimetry,
sea-ice concentration) carry only `(time, lat, lon)`.

To clip the vertical axis at request time, pass `minimum_depth` /
`maximum_depth` in metres:

```python
CMEMS(
    ...,
    variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
    minimum_depth=0.0,         # surface
    maximum_depth=200.0,       # to 200 m
)
```

For surface-only datasets the depth kwargs are silently ignored by
the toolbox. For 4-D datasets, omitting them returns the full depth
range — which can be 50+ levels and considerably more data than a
surface-bound application needs.

## 5. File format

The toolbox supports two on-disk shapes:

- `file_format="netcdf"` (default) — one `.nc` per request. Single
  file, opens with `xarray.open_dataset` / `pyramids.netcdf.NetCDF`
  / `netCDF4.Dataset`. Right choice for small-to-medium subsets
  (regional, sub-decade).
- `file_format="zarr"` — one directory-store per request. Stores
  the same data as chunked Zarr suitable for very large requests
  (global, multi-decade) and lazy access. Read with
  `xarray.open_zarr` or `pyramids.zarr.Zarr`.

```python
CMEMS(
    ...,
    file_format="zarr",
)
```

## 6. Post-process the returned NetCDF with pyramids

The earthlens project's GIS backend is
[pyramids](https://github.com/Serapieum-of-alex/pyramids); use it to
open the returned NetCDF rather than reaching for `xarray` directly:

```python
from pathlib import Path

from pyramids.netcdf import NetCDF

paths = cmems.download()
nc = NetCDF.read_file(str(paths[0]), read_only=True)
list(nc.meta_data.variables)          # ['time', 'lat', 'lon', 'depth', 'thetao', ...]
thetao_attrs = nc.meta_data.variables["thetao"]
thetao_attrs.long_name                # 'Sea water potential temperature'
thetao_attrs.unit                     # 'degrees_C'
arr = nc.read_array("thetao")         # numpy ndarray (time, depth, lat, lon)
nc.close()
```

(The earthlens aggregator — `earthlens.aggregate.aggregate_netcdf`,
backed by `pyramids.netcdf.NetCDF` — currently accepts only the ECMWF
catalog row shape and a `time x lat x lon` layout. CMEMS rows and the
depth axis are gated by a pyramids-side generalisation tracked under
`planning/providers/tasks-pyramids.md`. Until that lands,
`CMEMS.download(aggregate=...)` raises `NotImplementedError` and
post-processing happens through `pyramids.netcdf.NetCDF` as above.)

## 7. Curated catalog versus uncurated ids

The curated catalog targets the highest-leverage marine datasets;
~600 toolbox-addressable ids are not curated. The two paths:

- **Curated**: `Catalog.get_variable(dataset_id, variable)` gives you
  `units` + `long_name` + the flux/state marker without ever
  calling the toolbox. Useful for autocompletion, schema validation,
  and downstream tooling (aggregator, plot labels).
- **Uncurated**: just pass the id. `copernicusmarine.subset()` does
  not care whether earthlens curates the id; the catalog lookup is a
  metadata convenience. The download itself succeeds.

To promote an uncurated id into the bundled catalog without
hand-writing the stanza, run:

```bash
pixi run -e dev python tools/cmems/refresh_cmems_catalog.py add-ids \
    <new_dataset_id>
```

which fetches `describe()`, emits the YAML stanza, appends it to
`cmems_data_catalog.yaml`'s `datasets:` block, and re-parses to fail
loud on malformed YAML.

## 8. Common error modes

- `AuthenticationError` — no credentials available, or the toolbox
  rejected them. See [Authentication](authentication.md).
- `copernicusmarine.DatasetNotFound` — the dataset id does not
  resolve (typo, renamed). Run
  `tools/cmems/audit_cmems_datasets.py --strict` to spot
  catalog drift against the live toolbox.
- `copernicusmarine.VariableDoesNotExistInTheDataset` — one of the
  variable short names is wrong. The audit tool catches this too
  (`status: partial`).
- `copernicusmarine.CoordinatesOutOfDatasetBounds` — the requested
  bbox falls outside the dataset's native domain (e.g. the
  Mediterranean reanalysis covers ~30-46°N, 5°W-37°E; requesting
  the Pacific raises). Clip the bbox to the dataset domain or pick
  a global product.
