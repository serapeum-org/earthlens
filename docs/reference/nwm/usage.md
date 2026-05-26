# National Water Model — usage

## Install

```bash
pip install earthlens[nwm]      # boto3 (unsigned S3); no credentials needed
```

`xarray` + `netcdf4` (for decoding the fetched NetCDF files) are **not**
required to fetch — they're a downstream follow-on.

## Request

`variables` maps **configuration → product list**. A configuration runs
on a set of UTC cycles and publishes forecast steps; `cycles=` /
`steps=` / `horizon=` select which to pull:

```python
from earthlens.earthlens import EarthLens

lens = EarthLens(
    data_source="nwm",                          # alias: "national-water-model"
    variables={"short_range": ["channel_rt"]},  # streamflow on the river network
    start="2026-05-25",                         # cycle-date range (date-only)
    end="2026-05-25",
    lat_lim=[25, 50],                            # informational (CONUS files)
    lon_lim=[-125, -66],
    path="out/nwm",
    cycles=[0],                                  # the 00Z run
    steps=[1, 2, 3],                             # forecast hours f001–f003
)
inventory = lens.download()   # DataFrame of fetched NetCDF files
```

- **Configuration** — a key of `variables`
  (`"short_range"`, `"medium_range_mem1"`). `NWMCatalog().get_config(key)`
  returns its cycles / horizon / products; an unknown key raises with a
  did-you-mean hint.
- **Products** — the list value; must be among the configuration's
  products. An **empty list selects all** of them. (For
  `medium_range_mem1` the ensemble member rides on the token, so its
  products are `channel_rt_1` / `land_1`.)
- **`cycles=`** — restrict the run hours (subset of the config's
  `cycles_utc`); defaults to every cycle the config runs. Combined with
  the `start`/`end` date range, this is the cycle grid fetched.
- **`steps=` / `horizon=`** — explicit forecast steps win; otherwise
  `horizon=` expands from the config's first step; otherwise just the
  first step (`f001`).

## Output

`download()` returns a `pandas.DataFrame` with one row per fetched file:

| column | meaning |
|--------|---------|
| `config` | configuration key |
| `cycle` | run datetime (UTC) |
| `step` | forecast step (hours) |
| `valid_time` | `cycle + step` (UTC) |
| `product` | product token |
| `domain` | spatial domain (`conus`, …) |
| `path` | local NetCDF file |

The NetCDF files are written to `path`; read `channel_rt` streamflow with
`xarray.open_dataset(...)` (variables are indexed by `feature_id`, the
NHDPlus reach id).

!!! note "Recent dates only"
    The `noaa-nwm-pds` bucket retains a rolling window of recent days, so
    requests should target the last few days. A `(cycle, step)` not yet
    published — or a product a configuration does not carry on that cycle
    — is skipped with a warning rather than aborting the batch.

`aggregate=` is **not supported** (NWM `channel_rt` is feature-id
indexed, not a griddable raster — the facade rejects it for this
`tabular` backend).
