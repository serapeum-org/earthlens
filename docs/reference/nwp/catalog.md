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

## Curated models

The **MVP 5** are live-validated end to end; the **expanded set** is
metadata-curated from Herbie's templates + provider docs and should be
vetted with `tools/nwp/probe_nwp_model.py <key>` before relying on it
(the `errors="warn"` fetch policy skips any `(cycle, step)` a model
doesn't carry).

| Model key | Provider | Backend | Cycles (UTC) | Horizon | Notes |
|-----------|----------|---------|--------------|---------|-------|
| `gfs` | NOAA NODD | `herbie` | 00/06/12/18 | 384 h | 0.25° global · MVP |
| `gefs` | NOAA NODD | `herbie` | 00/06/12/18 | 384 h | 0.5° ensemble (`atmos.5`) · MVP |
| `hrrr` | NOAA NODD | `herbie` | hourly | 48 h¹ | 3 km CONUS (`wrfsfcf`) · MVP |
| `ifs-hres` | ECMWF Open Data | `ecmwf-opendata` | 00/06/12/18 | 240 h | 0.25° global · MVP |
| `icon-global` | DWD Open Data | `direct-https` | 00/06/12/18 | 180 h | icosahedral (raw fetch) · MVP |
| `rap` | NOAA NODD | `herbie` | hourly | 51 h | 13 km CONUS (`awp130pgrb`) |
| `nam` | NOAA NODD | `herbie` | 00/06/12/18 | 84 h | 12 km CONUS (`awphys`) |
| `nbm` | NOAA NODD | `herbie` | hourly | 264 h | National Blend (`co`) |
| `rrfs` | NOAA NODD | `herbie` | hourly | 60 h | 3 km (`prslev`) |
| `gdps` | ECCC MSC | `herbie` | 00/12 | 240 h | 15 km global |
| `rdps` | ECCC MSC | `herbie` | 00/06/12/18 | 84 h | 10 km regional |
| `hrdps` | ECCC MSC | `herbie` | 00/06/12/18 | 48 h | 2.5 km (`continental`) |
| `icon-eu` | DWD Open Data | `direct-https` | 00/06/12/18 | 120 h | **regular lat-lon (croppable)** ✓ probed |
| `icon-d2` | DWD Open Data | `direct-https` | every 3 h | 48 h | icosahedral (raw fetch) ✓ probed |
| `ens` | ECMWF Open Data | `ecmwf-opendata` | 00/06/12/18 | 360 h | IFS ENS control (`enfo`/`cf`) ✓ probed |
| `aifs` | ECMWF Open Data | `ecmwf-opendata` | 00/06/12/18 | 360 h | data-driven (`aifs-single`) ✓ probed |
| `rtma` | NOAA NODD | `herbie` | hourly | 0 h² | 2.5 km CONUS analysis (`anl`) |
| `urma` | NOAA NODD | `herbie` | hourly | 0 h² | 2.5 km CONUS analysis (`anl`) |
| `hiresw-arw` | NOAA NODD | `herbie` | 00/12 | 48 h | 2.5 km ARW window (`domain=conus`) |
| `href` | NOAA NODD | `herbie` | 00/06/12/18 | 48 h | ensemble mean (`domain=conus`) |
| `arpege-world` | Météo-France | `meteofrance-api` | 00/06/12/18 | 102 h | 0.25° global · WCS API (key)³ |
| `arome-france` | Météo-France | `meteofrance-api` | every 3 h | 51 h | 0.025° France · WCS API (key)³ |

³ **Météo-France WCS API.** Served from the authenticated portal
(`portail-api.meteofrance.fr`), not the static-only `mf-nwp-models` S3 bucket.
Set an application API key in `METEO_FRANCE_API_KEY` (or `MF_API_KEY`); the
centre issues OGC WCS 2.0.1 `GetCoverage` requests with server-side bbox
subsetting. The coverage-id strings are best-effort (not live-validated against
a key) and live in the catalog `request_options`/`bands`, so a fix is a catalog
edit — confirm with `probe_nwp_model.py arpege-world`.

² **Analyses** (`rtma`/`urma`) have `horizon_h=0` — they are valid *at* the cycle
time, so only the analysis step (`f000`) is fetched.

Not shipped: **CFS** (seasonal — a different time axis than `(cycle, step)`) and
**HAFS** (hurricane model — needs a storm id, not a generic `(cycle, step)`
raster). The unsigned `direct-boto3` Météo-France centre also exists for any
future static/unsigned MF mirror, but the live forecasts use `meteofrance-api`
above.

¹ **HRRR per-cycle horizon.** The `horizon_h` is the *maximum*: HRRR runs to
48 h only at the 00/06/12/18 synoptic cycles; the other (hourly) cycles run to
18 h. The catalog stores a single `horizon_h`, so a long-lead request on an
off-synoptic cycle asks for steps that cycle doesn't carry — those are skipped
under the default `errors="warn"` policy (see [usage](usage.md#partial-availability-errors)),
not fetched. Per-cycle horizons are a future catalog enhancement.

Each model maps the shared earthlens parameter names to its own
selector:

| Parameter | NOAA / ECCC (Herbie regex) | ECMWF (token) | DWD (token, lc for D2) |
|-----------|----------------------------|---------------|------------------------|
| `temperature_2m` | `:TMP:2 m above ground:` | `2t` | `T_2M` |
| `precipitation_acc` | `:APCP:surface:` | `tp` | `TOT_PREC` |
| `dewpoint_2m` | `:DPT:2 m above ground:` | `2d` | `TD_2M` |
| `relative_humidity_2m` | `:RH:2 m above ground:` | `2r` | `RELHUM_2M` |
| `wind_u_10m` | `:UGRD:10 m above ground:` | `10u` | `U_10M` |
| `wind_v_10m` | `:VGRD:10 m above ground:` | `10v` | `V_10M` |
| `wind_gust` | `:GUST:surface:` | `10fg` | `VMAX_10M` |
| `pressure_msl` | `:PRMSL:mean sea level:` | `msl` | `PMSL` |
| `surface_pressure` | `:PRES:surface:` | `sp` | `PS` |
| `total_cloud_cover` | `:TCDC:entire atmosphere:` | `tcc` | `CLCT` |
| `cape` | `:CAPE:surface:` | `cape` | `CAPE_ML` |
| `downward_shortwave_radiation` | `:DSWRF:surface:` | `ssrd` | — (no single token) |

11–12 surface bands per forecast model (analyses `rtma`/`urma` carry the 9
non-flux fields). Not every model publishes every band — a requested band a
model doesn't carry is skipped under `errors="warn"` (it is not a hard error).
The DWD tokens are HEAD-validated live for `icon-eu`; the ECMWF flux tokens
(`cape`/`ssrd`) may be outside the open-data subset for some runs and skip.

### Pressure-level (3-D) fields

Every forecast model (all centres) also exposes 3-D fields —
geopotential height, temperature, u-/v-wind, and relative humidity at
**925 / 850 / 700 / 500 / 300 / 250 / 200 / 100 hPa** (so ~52 bands each):

| Parameter | NOAA / ECCC (Herbie) | ECMWF (`param@level`) |
|-----------|----------------------|-----------------------|
| `geopotential_height_500hPa` | `:HGT:500 mb:` | `gh@500` |
| `temperature_850hPa` | `:TMP:850 mb:` | `t@850` |
| `wind_u_250hPa` | `:UGRD:250 mb:` | `u@250` |
| `relative_humidity_850hPa` | `:RH:850 mb:` | `r@850` |

All 20 forecast models carry these at eight pressure levels (the analyses
`rtma`/`urma` stay surface-only). The per-centre mechanics differ:

* **Herbie (NOAA/ECCC)** — the level is in the `.idx` regex (`:TMP:850 mb:`); no
  special handling.
* **ECMWF** — `param@level` (`t@850`) sets `levtype=pl` + `levelist`; the centre
  issues one `retrieve` per level type/level and concatenates (ecmwf-opendata
  can't mix level types in one request).
* **DWD ICON** — pressure-level data is a separate file
  (`…/pressure-level/…_{level}_{VAR}`), so each ICON row has a
  `request_options.pl_url_template` and `VAR@level` bands (`FI` = geopotential).
  HEAD-validated live for `icon-eu` (`T@850` / `FI@500` / `U@250` all 200).
* **Météo-France** — `COVERAGE@level` selects the `…__ISOBARIC_SURFACE` coverage
  + a WCS `subset=pressure(level×100 Pa)` (best-effort, key needed to validate).

```python
from earthlens.nwp import Catalog

cat = Catalog()
len(cat.datasets)                          # 22
cat.get_model("gfs").bands["temperature_2m"]   # ':TMP:2 m above ground:'
```

## Tooling

Three maintenance scripts live under `tools/nwp/`:

* `refresh_nwp_catalog.py` — print a model summary; `--live` HEAD-probes
  each `direct-https` model's latest cycle.
* `audit_nwp_catalog.py` — static consistency lint (backend vs
  `url_template` / `model_family`, empty bands, cycle-hour range).
* `probe_nwp_model.py <key>` — live availability check for one model,
  dispatching on backend (HTTP HEAD / unsigned S3 `head_object` /
  `Client.latest()` / Herbie URL resolution). Use it to vet a catalog
  row before relying on it.

```bash
pixi run -e dev python tools/nwp/refresh_nwp_catalog.py --live
pixi run -e dev python tools/nwp/audit_nwp_catalog.py
```
