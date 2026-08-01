# ECMWF — the three Copernicus Data Stores (CDS + ADS + EWDS)

The `earthlens.ecmwf` backend is a **single facade key (`ecmwf`)** that reaches all three Copernicus Data Store
instances through the same `cdsapi` client. Each dataset carries an `endpoint` that routes it to the right store;
one Personal Access Token authenticates against all three.

| Store | Programme | `endpoint` | Content |
|-------|-----------|-----------|---------|
| **CDS** | C3S — Climate Change | `cds` (default) | ERA5, CARRA/CERRA, seasonal, CMIP5/6, CORDEX, satellite CDRs, in-situ |
| **ADS** | CAMS — Atmosphere | `ads` | Air quality, greenhouse gases, fire emissions (GFAS), composition reanalysis/forecasts |
| **EWDS** | CEMS — Emergency | `ewds` | GloFAS + EFAS river discharge / flood, fire danger (FWI) |

## Credentials + the licence wall

- **One token, three stores.** The same Personal Access Token in `~/.cdsapirc` (or `CDSAPI_KEY`) authenticates
  against CDS, ADS, and EWDS — the backend only switches the URL. Nothing else to configure.
- **Per-store site policies + per-dataset licences are manual.** Each store requires a one-time acceptance of its
  site terms, and each dataset requires accepting its licence, **in the web portal** — this cannot be automated.
  Until they are accepted, a retrieve returns a clear `PermissionError` naming the dataset page. In particular,
  **ADS needs its account-level site policies accepted** (`ads.atmosphere.copernicus.eu`) before *any* ADS
  retrieve works.

## Discover anything

`earthlens datasets refresh ecmwf --write` enumerates all three stores' public catalogues into the per-store
`available_datasets:` index, and `earthlens datasets audit ecmwf --coverage` reports curated (DONE) vs reachable
(addressable) coverage across the stores.

## Download anything — the raw-request passthrough

Any dataset in any store is downloadable by id + a raw request, with **no curated row** — the coverage lever:

```python
from earthlens import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    dataset="reanalysis-era5-single-levels",      # any CDS / ADS / EWDS id
    request={                                     # the store's own request dict
        "variable": ["2m_temperature"],
        "year": ["2023"], "month": ["01"], "day": ["01"],
        "time": ["00:00"], "area": [1, 0, 0, 1],
        "data_format": "netcdf",
    },
    # endpoint="ads",                             # auto-resolved from the index if omitted
    path="data/passthrough",
)
lens.download()
```

The endpoint auto-resolves from the per-store index (or a curated row); NetCDF (including single-member
`netcdf_zip`) is unwrapped, multi-member zip-of-NetCDF is unpacked to its members, GRIB is left readable, and
CSV/point responses are written raw for you to read directly (e.g. `pandas.read_csv`).

## Curated ergonomics — typed rows

Curated rows add typed variables, defaults, and constraint pre-validation. Each row carries a `request_kind` that
shapes the request for its schema family:

| `request_kind` | Shape | Families |
|----------------|-------|----------|
| `form` | year/month/day (+ time) | ERA5, CARRA, EFAS forecast |
| `glofas` | year/month/day + `leadtime_hour`, no time | GloFAS forecast |
| `glofas_hindcast` | hyear/hmonth/hday + lead | GloFAS/EFAS reforecast, EFAS historical |
| `seasonal` / `seasonal_hindcast` | year/month (or hyear/hmonth) + lead, no day | GloFAS/EFAS seasonal (+ reforecast) |
| `cams_date` | a single `date` range string + time | EAC4, composition forecasts, GFAS, EU air-quality forecasts |
| `cams_inversion` | year/month, no day/time/area | GHG inversion, EU air-quality reanalyses |
| `fire` | year/month/day + `grid`, no time | CEMS fire danger |
| `satellite_cdr` | year/month/day + sensor/version selectors, zip output | satellite CDRs (soil-moisture, precip, SST) |

## Curation tooling

`earthlens datasets curate ecmwf <id>` seeds a loader-valid row from the live `form.json`: it resolves the store,
guesses the `request_kind`, and enumerates the first variable. Fill in the `nc_variable` / `units` from a live
retrieve and paste it into the right `catalog/*.yaml` shard.

See [EWDS (GloFAS / floods)](ewds.md) for the flood-specific walkthrough and
[Catalog & tooling](catalog.md) for the catalog layout.
