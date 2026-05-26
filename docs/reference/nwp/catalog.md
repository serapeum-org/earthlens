# NWP forecasts — catalog & install

## Installation

The NWP backend's SDKs are an optional extra:

```bash
pip install earthlens[nwp]      # herbie-data + ecmwf-opendata
```

Two binary libraries are **environment requirements** (not on PyPI in a
usable form on every platform), so install them via conda-forge if they
are missing:

* **`libgdal-grib`** — the GDAL GRIB driver that
  `pyramids.grib.open_grib` calls. Bundled in the `pyramids-gis` wheels;
  raises `DriverNotExistError` if absent.
* **`eccodes`** — the C library that `cfgrib`/`eccodes` need. Herbie's
  import chain pulls `cfgrib`, so `import herbie` fails with
  `RuntimeError: Cannot find the ecCodes library` if the binary is
  missing (notably the pip `eccodes` wheel on Windows). earthlens itself
  imports neither `cfgrib` nor `eccodes` directly.

```bash
conda install -c conda-forge eccodes libgdal-grib
```

## Curated models (MVP)

| Model key | Provider | Backend | Cycles (UTC) | Horizon | Notes |
|-----------|----------|---------|--------------|---------|-------|
| `gfs` | NOAA NODD | `herbie` | 00/06/12/18 | 384 h | 0.25° global |
| `gefs` | NOAA NODD | `herbie` | 00/06/12/18 | 384 h | 0.5° ensemble (`atmos.5`) |
| `hrrr` | NOAA NODD | `herbie` | hourly | 48 h¹ | 3 km CONUS (`wrfsfcf`) |
| `ifs-hres` | ECMWF Open Data | `ecmwf-opendata` | 00/06/12/18 | 240 h | 0.25° global |
| `icon-global` | DWD Open Data | `direct-https` | 00/06/12/18 | 180 h | native icosahedral grid |

¹ **HRRR per-cycle horizon.** The `horizon_h` is the *maximum*: HRRR runs to
48 h only at the 00/06/12/18 synoptic cycles; the other (hourly) cycles run to
18 h. The catalog stores a single `horizon_h`, so a long-lead request on an
off-synoptic cycle asks for steps that cycle doesn't carry — those are skipped
under the default `errors="warn"` policy (see [usage](usage.md#partial-availability-errors)),
not fetched. Per-cycle horizons are a future catalog enhancement.

Each model maps the shared earthlens parameter names to its own
selector:

| Parameter | GFS / GEFS / HRRR (Herbie regex) | IFS (ecmwf token) | ICON (DWD token) |
|-----------|----------------------------------|-------------------|------------------|
| `temperature_2m` | `:TMP:2 m above ground:` | `2t` | `T_2M` |
| `precipitation_acc` | `:APCP:surface:` | `tp` | `TOT_PREC` |

```python
from earthlens.nwp import Catalog

cat = Catalog()
sorted(cat.datasets)                       # ['gefs', 'gfs', 'hrrr', 'icon-global', 'ifs-hres']
cat.get_model("gfs").bands["temperature_2m"]   # ':TMP:2 m above ground:'
```

## Tooling

Two maintenance scripts live under `tools/nwp/`:

* `refresh_nwp_catalog.py` — print a model summary; `--live` HEAD-probes
  each `direct-https` model's latest cycle.
* `audit_nwp_catalog.py` — static consistency lint (backend vs
  `url_template` / `model_family`, empty bands, cycle-hour range).

```bash
pixi run -e dev python tools/nwp/refresh_nwp_catalog.py --live
pixi run -e dev python tools/nwp/audit_nwp_catalog.py
```
