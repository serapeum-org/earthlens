# ECMWF — ECDS + XDS (the ECMWF-hosted data stores)

Two stores beyond the Copernicus trio, reached through the **same** `earthlens.ecmwf` facade key and the **same**
`cdsapi` client — only the `endpoint` differs:

| Store | `endpoint` | API root | Datasets |
|-------|-----------|----------|----------|
| **ECDS** — ECMWF Data Store | `ecds` | `https://ecds.ecmwf.int/api` | `tigge-forecasts`, `s2s-forecasts`, `s2s-reforecasts` |
| **XDS** — ECMWF Cross Data Store | `xds` | `https://xds.ecmwf.int/api` | `derived-fire-fuel-biomass`, `projections-fire-fuel-burned-area` |

They are ECMWF-hosted rather than Copernicus-branded, but run the same CADS software and publish the same
`form.json` / `constraints.json` catalogue API, so pre-flight request validation works against them unchanged.

## Credentials — one token, five stores

The Personal Access Token in `~/.cdsapirc` (or `CDSAPI_KEY`) authenticates ECDS and XDS exactly as it does
CDS / ADS / EWDS — querying `profiles/v1/account` on each store returns the same account. There is **no separate
registration** and no new SDK.

Two acceptance gates are easy to miss:

- **ECDS requires a portal-scope policy**, `terms-of-use-ecds`, which is **separate from every dataset licence**.
  Checking only dataset licences will not reveal it. Without it, every ECDS retrieve fails with
  `403 … user didn't accept all required site policies`.
- **Licences are versioned.** Holding revision 4 of a licence does not satisfy a dataset that requires revision 5;
  the refusal names the dataset's `#manage-licences` page.

## TIGGE — multi-centre ensemble forecasts (ECDS)

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    variables={"tigge-forecasts": ["2m-temperature"]},
    start="2024-01-01",
    end="2024-01-01",
    temporal_resolution="daily",
    lat_lim=[50.0, 51.0],
    lon_lim=[9.0, 10.0],
    path="data/tigge",
)
lens.download()
```

!!! warning "TIGGE returns an unstructured grid, not lat/lon"
    The retrieved NetCDF carries a **`values`** dimension rather than `lat` / `lon`: TIGGE serves its native
    reduced-Gaussian representation, and the `area` selector **subsets points without regridding**. This is
    unlike every other curated ECMWF dataset in earthlens. If you need a regular grid, regrid downstream —
    earthlens does not silently do it for you.

TIGGE's request vocabulary is **not** the MARS idiom, and the live constraints are authoritative:

| Key | Valid values |
|-----|--------------|
| `origin` | `ecmwf` (**not** the MARS spelling `ecmf`), plus `bom`, `cma`, `cptec`, `dwd`, `eccc`, `imd`, `jma`, `kma`, `mf`, `ncep`, `ncmrwf`, `ukmo` |
| `level_type` | `single_level` (**not** `surface`), `pressure`, `isentropic`, `potential_vorticity` |
| `variable` | `2_m_temperature` (underscores around the `m`), and 36 others |

The curated row pins `origin: ecmwf`. To pull a **different contributing centre**, use the
[raw-request passthrough](datastores.md#download-anything--the-raw-request-passthrough), which takes the store's
own request dict verbatim:

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    dataset="tigge-forecasts",
    request={
        "origin": ["ukmo"],                 # any of the 13 contributing centres
        "variable": ["2_m_temperature"],
        "level_type": ["single_level"],
        "forecast_type": ["control_forecast"],
        "year": ["2024"], "month": ["01"], "day": ["01"],
        "time": ["00:00"], "leadtime_hour": ["24"],
        "area": [51, 9, 50, 10],
        "data_format": "netcdf",
    },
    path="data/tigge-ukmo",
)
lens.download()
```

## S2S — sub-seasonal to seasonal forecasts (ECDS)

S2S shares TIGGE's single-level ECMWF vocabulary, but — unlike TIGGE — comes back on a **regular
`latitude`/`longitude` grid**:

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    variables={"s2s-forecasts": ["2m-temperature"]},
    start="2026-08-01",
    end="2026-08-01",
    temporal_resolution="daily",
    lat_lim=[50.0, 51.0],
    lon_lim=[9.0, 10.0],
    path="data/s2s",
)
lens.download()
```

### Reforecasts have two date axes

`s2s-reforecasts` is the one row here that needs explaining. It carries **two** dates:

| Keys | Meaning |
|------|---------|
| `year` / `month` / `day` | the **model cycle** — which forecast system version produced the reforecast |
| `hyear` / `hmonth` / `hday` | the **reforecast date** — the historical date being re-forecast |

The store only serves a reforecast on the model run's **own calendar day**, so the two dates move together:
`request_kind: s2s_reforecast` copies the requested `month`/`day` into `hmonth`/`hday`. Only the reforecast
**year** is a per-row value (`hyear: 1995` by default); use the
[passthrough](datastores.md#download-anything--the-raw-request-passthrough) to target a different one.

!!! warning "One model-cycle date per request"
    Because a CDS form request treats every list as an independent cross-product axis, it cannot express the
    pairing between the two dates: an `n`-day window would submit `n x n` `day`/`hday` combinations of which
    only the `n` diagonal pairs exist. The backend therefore rejects a multi-day window for this row with a
    clear error — request one model-cycle date at a time (`start == end`).

!!! note "Why not `request_kind: glofas_hindcast`?"
    That kind looks like the obvious fit — it exists precisely to map `year` to `hyear` — but it **renames**
    rather than adds, so it would delete the model-cycle date this dataset also requires.

## Fire fuel and burned area (XDS)

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    variables={"derived-fire-fuel-biomass": ["live-fuel-moisture-content-group"]},
    start="2000-01-01",
    end="2000-01-31",
    temporal_resolution="monthly",
    lat_lim=[50.0, 51.0],
    lon_lim=[9.0, 10.0],
    path="data/fire-fuel",
)
lens.download()
```

Both XDS datasets are delivered as a **ZIP wrapping a single NetCDF** whose member name encodes the requested
subset (e.g. `LFMC_MAP_2000_01.area-subset.51.10.50.9.nc`); earthlens unwraps that for you, so `download()`
returns the `.nc` path.

Neither XDS dataset has a `day` or `time` selector, and `projections-fire-fuel-burned-area` is **annual** (no
`month` either). The curated rows drop those keys, so a monthly or annual request is all you need.

## Scope and follow-ups

Curated so far, each verified by a real retrieve rather than from the constraints alone:

| Dataset | Variable | NetCDF | Units |
|---------|----------|--------|-------|
| `tigge-forecasts` | `2m-temperature` | `t2m` | `K` |
| `s2s-forecasts` | `2m-temperature` | `t2m` | `K` |
| `s2s-reforecasts` | `maximum-2m-temperature-in-the-last-6-hours` | `mx2t6` | `K` |
| `derived-fire-fuel-biomass` | `live-fuel-moisture-content-group` | `LFMC` | `%` |
| `projections-fire-fuel-burned-area` | `burned-area` | `BAF_pred` | `1` (CF dimensionless — the file declares CF-1.9 with `long_name` "Burned Area Fraction" and values inside [0, 1]) |

**Deliberately left uncurated:**

- **The other variables each dataset exposes** — TIGGE alone offers 37. Only the rows above have been retrieved
  and unit-verified; placeholder units have shipped wrong values before, so the rest wait for a real download.
  Any of them is reachable today through the
  [raw-request passthrough](datastores.md#download-anything--the-raw-request-passthrough).

See [the five data stores](datastores.md) for the cross-store picture and
[EWDS (GloFAS / floods)](ewds.md) for the flood walkthrough.
