# NASA Earthdata — usage

The Earthdata backend is reached through the
[`EarthLens`](../earthlens.md) facade with `data_source="earthdata"`, or
directly as `earthlens.earthdata.EarthData`.

## Quickstart

```python
from earthlens import EarthLens

el = EarthLens(
    data_source="earthdata",
    start="2020-06-01",
    end="2020-06-02",
    variables={"GPM_3IMERGHHL_07": ["precipitation"]},
    lat_lim=[0.0, 10.0],
    lon_lim=[30.0, 40.0],
    path="./out",
)
paths = el.download()   # native granules written under ./out
```

`download()` returns the list of local granule paths (or in-region S3
handles — see below).

## The request shape

- **`variables`** is a `{dataset_key: [band, ...]}` mapping. The
  `dataset_key` is a curated entry from the [Catalog](catalog.md) (e.g.
  `"GPM_3IMERGHHL_07"`); `EarthData` resolves it to a `short_name` /
  `version` / `provider` for the CMR search.
- **Bands are informational.** The MVP fetches *whole granules*, so you
  receive the entire file regardless of which bands you list. The band
  list seeds catalog metadata and the future server-side subset; it does
  not slice the download.
- **Multiple datasets** may be requested in one call — but they must all
  share one `output_kind` (see below). A `lat_lim` / `lon_lim` bounding
  box and a `start` / `end` window scope the CMR search.

## Per-dataset output kind

`EarthData.OUTPUT_KIND` is set **per instance** from the resolved
catalog row — `raster`, `vector`, or `tabular`. A single request mixing
kinds (e.g. a raster GPM product and a vector GEDI product) is rejected
at construction, because one instance carries exactly one output kind.
Split such a request into one call per kind.

```python
el = EarthLens(data_source="earthdata",
               variables={"GEDI04_A_002": ["agbd"]}, ...)
el.datasource.OUTPUT_KIND   # 'vector'
```

## Cloud streaming vs HTTPS download

The `direct_s3` kwarg controls how granules are fetched:

| `direct_s3` | Behaviour |
|-------------|-----------|
| `"auto"` (default) | Stream from S3 (`earthaccess.open`) **only** when the collection is cloud-hosted **and** you run in the DAAC's region (`us-west-2`); otherwise download over HTTPS. |
| `"always"` | Always stream from S3 (use when you know you are in-region). |
| `"never"` | Always download over HTTPS (the safe choice off-cloud). |

Your region is read from the `region=` kwarg, then `AWS_REGION`, then
`AWS_DEFAULT_REGION`. The backend never probes EC2 instance metadata, so
it will not hang off-cloud.

## Disambiguating a DAAC

If a `short_name` is served by more than one provider, pass `daac=` to
pick the curated row you mean:

```python
EarthLens(data_source="earthdata", daac="GES_DISC", ...)
```

`daac=` only applies to a **single-dataset** request — it disambiguates
one colliding `short_name`. Combining it with a multi-dataset
`variables` mapping is rejected at construction (it would be applied to
every key and wrongly drop any whose DAAC differs); issue one request
per dataset instead.

## Aggregation

Because `OUTPUT_KIND` is per-instance, the facade forwards an
`aggregate=AggregationConfig(...)` request **only for raster datasets**
and rejects it (`NotImplementedError`) for vector / tabular ones.

The reduction is **axis-driven**: the common case — a stack of
single-timestep granules — is windowed by `config.freq` via pyramids'
`DatasetCollection.groupby`; a single granule that already carries a
multi-timestep internal time axis is collapsed via `NetCDF.reduce`.

```python
from earthlens.aggregate import AggregationConfig

el = EarthLens(data_source="earthdata",
               variables={"GPM_3IMERGM_07": ["precipitation"]}, ...)
monthly = el.download(aggregate=AggregationConfig(freq="1MS"))
```
