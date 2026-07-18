# ECMWF — EWDS (CEMS Early Warning Data Store)

The ECMWF backend reaches more than one Copernicus Data Store instance through
the same `cdsapi` client. Besides the Climate Data Store (CDS), it can retrieve
from the **CEMS Early Warning Data Store (EWDS)** — the home of GloFAS (Global
Flood Awareness System), EFAS, and CEMS fire danger. Each is a separate CADS
instance at its own URL; the catalog routes a dataset to the right one via an
`endpoint` field (`cds` by default, `ewds` for the datasets below).

## Credentials — one token, three stores

EWDS uses the **same Copernicus single sign-on** as CDS, and the **same Personal
Access Token authenticates against CDS, ADS, and EWDS** — only the URL differs.
So if your `~/.cdsapirc` already works for CDS, it works for EWDS too; you do not
need a second token. You do, however, have to **accept each EWDS dataset's
licence** once, on its dataset page.

1. Sign in at <https://ewds.climate.copernicus.eu/> (your CDS account works).
2. Open the dataset page — e.g.
   <https://ewds.climate.copernicus.eu/datasets/cems-glofas-forecast> — and tick
   the licence at the bottom of the **Download** tab. Acceptance is permanent.
3. Nothing else to configure: the backend reuses your CDS token from
   `~/.cdsapirc` (or `CDSAPI_KEY`). To point EWDS at a different token or URL,
   set `EWDS_KEY` / `EWDS_URL`.

If no token can be resolved for a non-CDS endpoint, the backend raises
`AuthenticationError` naming the `EWDS_KEY` env var and the EWDS profile page.

## GloFAS river-discharge forecast

`cems-glofas-forecast` delivers GloFAS river discharge on its native **regular
0.05° WGS84 grid**, as NetCDF (variable `dis24`). The backend snaps the bbox to
that 0.05° grid automatically (regular CDS datasets keep their 0.125° ERA5
snap). Request it with `temporal_resolution="daily"`:

```python
from earthlens.core import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    variables={
        "cems-glofas-forecast": ["river-discharge-in-the-last-24-hours"],
    },
    start="2026-07-01",
    end="2026-07-01",
    temporal_resolution="daily",
    lat_lim=[0.0, 1.0],
    lon_lim=[0.0, 1.0],
    path="data/glofas",
)
lens.download()   # retrieves via the EWDS endpoint; writes one NetCDF
```

The row ships sensible defaults in its catalog `extras` — `system_version:
operational`, `hydrological_model: lisflood`, `leadtime_hour: 24`,
`product_type: control_forecast`, delivered `unarchived` as NetCDF. The output
lands at `<path>/river_discharge_in_the_last_24_hours_cems-glofas-forecast.nc`.

## Scope and follow-ups

Only `cems-glofas-forecast` is curated in this release (verified end to end
against live EWDS). The rest of the EWDS universe is reachable with the same
token but not yet wired for download:

| Dataset | Status / blocker |
|---|---|
| `cems-glofas-forecast` | **Shipped** — verified live. |
| `cems-glofas-reforecast`, `cems-glofas-historical` | Use `hyear`/`hmonth`/`hday` date keys — need a date-key remap. |
| `cems-glofas-seasonal`, `cems-glofas-seasonal-reforecast` | Monthly + `leadtime_hour` shape. |
| `cems-fire-historical-v1`, `cems-fire-seasonal` | Need their valid `product_type` / `dataset_type` / `system_version` combos pinned. |
| `efas-*` | The public EFAS archive (licence-gated); EFAS real-time is partner-gated and out of scope. |

See [Usage](usage.md) for the general request shape and
[Catalog & tooling](catalog.md) for the catalog layout.
