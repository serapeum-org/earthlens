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
from earthlens.core import EarthLens

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
| `fire` | year/month/day, no time (historical adds `grid` + `dataset_type`; seasonal adds `leadtime_hour`) | CEMS fire danger |
| `satellite_cdr` | year/month/day + sensor/version selectors, zip output | satellite CDRs (soil-moisture, precip, SST) |

## Curation tooling

`earthlens datasets curate ecmwf <id>` seeds a loader-valid row from the live `form.json`: it resolves the store,
guesses the `request_kind`, and enumerates **every** variable the form exposes (each `nc_variable` / `units` seeded
as an `unknown` placeholder). With `--write` the row is spliced into the correct `catalog/*.yaml` shard automatically
(auto-categorised from the id prefix — `reanalysis-era5-*` → `era5.yaml`, `cams-*` → `ads.yaml`, `cems-fire-*` →
`fire.yaml`, and so on; pass `--target <stem>` to override).

Two bulk modes drive the whole catalog at once (both require `--write`, and both are idempotent — safe to re-run):

- **`earthlens datasets curate ecmwf --all --write [--limit N]`** — bulk-seed every *uncurated* dataset. The uncurated
  set is `available_datasets − datasets` (the reachable-but-unmodelled ids from `audit ecmwf --coverage`); each is
  seeded from its live `form.json` and filed into its family shard. A dataset already curated, or whose form fetch
  fails, is skipped. Run `refresh ecmwf --write` first so the `available_datasets:` index is populated.
- **`earthlens datasets curate ecmwf --fill-empty --write [--limit N]`** — bulk-hydrate the placeholders. For every
  curated row still carrying a `units: unknown` variable, it retrieves a tiny NetCDF via `cdsapi` (`~/.cdsapirc`) and
  splices the real `nc_variable` / `units` into the stanza in place, leaving the surrounding rows untouched. It is
  licence-gated and best-effort: a dataset whose licence is unaccepted (or whose retrieve fails) is skipped, not fatal,
  so the fill is partial by design — re-run it as you accept more per-dataset licences.

The end-to-end flow for onboarding the full inventory is therefore: `refresh ecmwf --write` (index the stores) →
`curate ecmwf --all --write` (seed every uncurated id) → `curate ecmwf --fill-empty --write` (hydrate the placeholders
from live retrieves).

See [EWDS (GloFAS / floods)](ewds.md) for the flood-specific walkthrough and
[Catalog & tooling](catalog.md) for the catalog layout.
