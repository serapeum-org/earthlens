# NWP forecasts — catalog & install

## Installation

The NWP backend's SDKs are an optional extra:

```bash
pip install earthlens[nwp]      # herbie-data + ecmwf-opendata
```

That is the whole install — there is nothing to fetch from conda-forge.

The GRIB **read** path is pyramids': `pyramids.grib.open_grib` uses the driver
bundled in the `pyramids-gis` wheel, so nothing has to be installed alongside
it.

The **decode** libraries Herbie needs (`cfgrib`, `eccodes`) come in as ordinary
PyPI dependencies of `herbie-data`, and the ecCodes binary they bind to arrives
with them: on Windows through `ecmwflibs`, which the `nwp` extra installs for
that platform, and elsewhere in the `eccodes` wheel itself. earthlens imports
neither `cfgrib` nor `eccodes` directly.

!!! note "If `import herbie` cannot find ecCodes"
    Older `eccodes` wheels did not bundle the binary, and a mixed conda/pip
    environment can still leave the binding unable to locate it — the symptom
    is `RuntimeError: Cannot find the ecCodes library`. Upgrading the wheel
    (`pip install -U eccodes`) is normally enough. On Windows, a conda-provided
    library is installed as `Library\bin\eccodes.dll` while the pip binding's
    `findlibs` looks for `lib\libeccodes.dll`; copying it to the expected name
    inside the environment prefix resolves that case.

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
| `gdps` | ECCC MSC | `eccc-msc` | 00/12 | 240 h | 15 km global, direct Datamart (WIS2 tokens, 30 d retention) |
| `rdps` | ECCC MSC | `eccc-msc` | 00/06/12/18 | 84 h | 10 km regional, direct Datamart (WIS2 tokens, 30 d retention) |
| `hrdps` | ECCC MSC | `eccc-msc` | 00/06/12/18 | 48 h | 2.5 km continental, direct Datamart (WIS2 tokens, 30 d retention) |
| `geps` | ECCC MSC | `eccc-msc` | 00/12 | 384 h | 0.5° ensemble (21 members per `_allmbrs` file), 14 d retention |
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
| `nam-conusnest` | NOAA NODD | `herbie` | 00/06/12/18 | 60 h | NAM 5 km CONUS nest |
| `hiresw-fv3` | NOAA NODD | `herbie` | 00/12 | 48 h | HiResW FV3 2.5 km (`domain=conus`) |
| `nbm-ak` / `nbm-hi` / `nbm-pr` | NOAA NODD | `herbie` | hourly | 264 h | NBM Alaska / Hawaii / Puerto Rico |
| `icon-eps` | DWD Open Data | `direct-https` | 00/06/12/18 | 180 h | ICON-EPS ensemble (all members/file; icosahedral; surface-only) |
| `icon-eu-eps` | DWD Open Data | `direct-https` | 00/06/12/18 | 120 h | ICON-EU-EPS ensemble (icosahedral; surface-only) |
| `icon-d2-eps` | DWD Open Data | `direct-https` | every 3 h | 48 h | ICON-D2-EPS ensemble (icosahedral; surface-only) |
| `ens-mean` / `ens-spread` | ECMWF Open Data | `ecmwf-opendata` | 00/06/12/18 | 360 h | ENS mean (`type=em`) / spread (`type=es`) |

**32 models, ~2100 band-entries.** Not catalog-curatable (different data model —
each needs its own backend): **NWM** (hydrology), **MRMS** / **NEXRAD** (radar),
**CFS** (seasonal axis), **HAFS** (per-storm). The DWD ICON-EPS / ECMWF ENS rows
above give the ensemble; GEFS/ENS also expose individual members via `members=`
(see [usage](usage.md#ensemble-members)).

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
**13 isobaric levels: 1000 / 925 / 850 / 700 / 600 / 500 / 400 / 300 / 250 /
200 / 150 / 100 / 50 hPa** (so ~77 bands each on the global models; the
ICON-EPS ensembles are surface-only):

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
len(cat.datasets)                          # 32
cat.get_model("gfs").bands["temperature_2m"]   # ':TMP:2 m above ground:'
```

## Tooling

The `earthlens datasets` verbs keep the catalog honest:

* `list nwp` / `show nwp <key>` — the model summary (backend, cycles,
  horizon, bands).
* `validate nwp` — static consistency lint (`url_template` / band map /
  cycle-hour range).
* `validate nwp --live` — HEAD-probes each `direct-https` model's latest
  expected cycle URL to confirm the source is serving.
* `probe nwp <key>` — read the model's GRIB `.idx` and report which catalog
  band tokens are present. Use it to vet a row before relying on it.

```bash
earthlens datasets validate nwp --live
earthlens datasets probe nwp icon-global
```
