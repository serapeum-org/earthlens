# ECMWF / Copernicus CDS — usage

## Request shape

The ECMWF backend takes a `variables` mapping of **dataset short name → list of
variable codes**, plus a date range, a bbox, and a `temporal_resolution`:

```python
from earthlens import EarthLens

lens = EarthLens(
    data_source="ecmwf",
    variables={
        "reanalysis-era5-single-levels": ["2m-temperature"],
    },
    start="2022-01-01",
    end="2022-01-03",                 # inclusive date range
    temporal_resolution="daily",      # "daily" | "monthly"
    lat_lim=[4.0, 5.0],
    lon_lim=[-75.0, -74.0],
    path="data/era5",
)
lens.download()                       # blocks on the CDS queue + retrieve (~1–10 min)
```

Each variable code resolves through the bundled catalog to the CDS request name
(`2m-temperature` → `2m_temperature`), the NetCDF short name used to read the
array back (`t2m`), and the dataset's `product_type` / pressure-level defaults.
The download writes one NetCDF per variable to
`<path>/<cds_variable>_<dataset>.nc`. See [Catalog & probe tooling](catalog.md)
for the available datasets and variable codes.

## Temporal resolution

`temporal_resolution` is **purely a request-shape selector** — it does not
change which dataset or variables you get:

| value | effect |
|-------|--------|
| `"daily"` | `freq="D"` date axis; per-day `time` slots requested |
| `"monthly"` | monthly axis; routes to the dataset's monthly-means sibling where one is declared |

## Skipping the pre-flight constraint check

By default the backend validates each request against the dataset's
`constraints.json` before submitting, so a bad request fails locally instead of
in the CDS queue. Bypass it when you know the request is valid or constraints
are unavailable:

```python
lens = EarthLens(
    data_source="ecmwf",
    variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
    start="2022-01-01", end="2022-01-03",
    lat_lim=[4.0, 5.0], lon_lim=[-75.0, -74.0], path="data/era5",
    skip_constraints=True,            # forwarded to the backend
)
```

`skip_constraints` is one of the extra keyword arguments the `EarthLens` facade
forwards verbatim to the backend constructor.

## Multiple datasets and variables

A single request can mix datasets and ask for several variables each; the
backend fans out one CDS retrieve per `(dataset, variable)`:

```python
variables = {
    "reanalysis-era5-single-levels": ["2m-temperature", "total-precipitation"],
    "reanalysis-era5-land": ["2m-temperature"],
}
```

## Aggregating the downloaded stack

Pass `aggregate=` to reduce the per-variable NetCDF into windowed composites
(e.g. daily means, monthly sums). For accumulated **flux** variables (total
precipitation, evaporation, radiation) the catalog's `types: flux` marking lets
`op="auto"` route to a `sum`; instantaneous **state** variables route to a
`mean`:

```python
from earthlens.aggregate import AggregationConfig

lens = EarthLens(
    data_source="ecmwf",
    variables={"reanalysis-era5-single-levels": ["total-precipitation"]},
    start="2022-01-01", end="2022-01-31",
    temporal_resolution="daily",
    lat_lim=[4.0, 5.0], lon_lim=[-75.0, -74.0], path="data/era5",
)
lens.download(
    aggregate=AggregationConfig(freq="1MS", op="auto"),   # monthly totals
)
```

Aggregated GeoTIFFs land under `<path>/aggregated/`. See
[Aggregation](../aggregation.md) for the full `op="auto"` walkthrough and the
flux-vs-state distinction.

## Notebook examples

Runnable notebooks live under **Examples → CDS / ECMWF**, including the
[quickstart](../../examples/ecmwf/cds_quickstart.ipynb) and a dozen
domain-specific recipes (hydrology, oceanography, solar/wind resource, drought,
heat waves, …).
