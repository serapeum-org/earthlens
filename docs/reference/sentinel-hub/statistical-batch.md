# Sentinel Hub — Statistical & Batch guide

This page covers the **tabular** plane (Statistical) and the **scale-out** planes
(Async, Batch, Batch-Statistical). For the synchronous raster path see
[Usage](usage.md).

## Statistical → tabular zonal stats

`api="statistical"` + `geometry=` computes zonal statistics over a polygon (or
every feature of a `FeatureCollection`) and writes a tidy CSV. Use a **stats**
recipe (or a custom evalscript with a `dataMask` band).

```python
from earthlens.core import EarthLens

polygon = {
    "type": "Polygon",
    "coordinates": [[[14.24, 40.80], [14.27, 40.80],
                     [14.27, 40.83], [14.24, 40.83], [14.24, 40.80]]],
}

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-01", end="2020-06-30",
    variables={"sentinel-2-l2a-ndvi-stats": []},
    lat_lim=[40.80, 40.83], lon_lim=[14.24, 14.27],
    path="out", resolution=20,
    api="statistical",
    geometry=polygon,
)
tables = el.download()      # -> ['out/sentinel-2-l2a-ndvi-stats.csv']
```

The output CSV has one row per (interval × output × band) with columns
`feature_id`, `interval_from`, `interval_to`, `output`, `band`, `min`, `max`,
`mean`, `stDev`, `sampleCount`, `noDataCount`, and the `p5` / `p50` / `p95`
percentiles. A `FeatureCollection` produces one request per feature and stamps
each feature's `id` (or `properties.id`, else its index) onto its rows.

Combine with `aggregate=` to get a time series — `freq` maps to the Statistical
`aggregation_interval`:

```python
from earthlens.aggregate import AggregationConfig
el.download(aggregate=AggregationConfig(freq="1MS", op="mean"))  # one row per month
```

## Async vs local tiling vs Batch

For a render larger than a single Process request (2500 px/side):

| Approach | `api=` | Needs S3? | Best for |
|---|---|---|---|
| Local tiling | `"tiling"` | No | Medium/large AOIs you want as one local GeoTIFF |
| Async | `"async"` | Yes | ≤ 10000 px/side, delivered to S3 |
| Batch | `"batch"` | Yes | Continental / global AOIs, server-side tiling |

When `api=` is omitted, the backend picks **tiling** if no `batch_output` is set,
otherwise async / batch by size.

## Configuring S3 delivery (`batch_output`)

The Async, Batch, and Batch-Statistical planes deliver server-side to **your S3
bucket**, which requires an **IAM role** the Sentinel Hub service can assume. See
the Sentinel Hub docs on
[Batch Processing](https://docs.sentinel-hub.com/api/latest/api/batch/) for the
bucket policy / IAM-role setup.

```python
batch_output = {
    "bucket": "s3://my-bucket/sh-output",
    "iam_role_arn": "arn:aws:iam::123456789012:role/sentinelhub",
    "grid_id": 2,            # a Sentinel Hub tiling grid id (Batch only)
}

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-01", end="2020-06-30",
    variables={"sentinel-2-l2a-ndvi": []},
    lat_lim=[10.0, 40.0], lon_lim=[0.0, 30.0],   # large AOI
    path="out", resolution=10,
    api="batch",
    batch_output=batch_output,
)
uris = el.download()        # -> ['s3://my-bucket/sh-output']
```

## Batch-Statistical (huge `FeatureCollection`s)

`api="batch-statistical"` computes zonal stats over a large `FeatureCollection`
uploaded to S3 as a GeoPackage ("mean NDVI per 50 000 farms"), asynchronously:

```python
batch_output = {
    "input_features": "s3://my-bucket/farms.gpkg",
    "bucket": "s3://my-bucket/stats-out",
    "iam_role_arn": "arn:aws:iam::123456789012:role/sentinelhub",
    "feature_ids": ["farm-1", "farm-2", "..."],
}

el = EarthLens(
    data_source="sentinel-hub",
    start="2020-06-01", end="2020-06-30",
    variables={"sentinel-2-l2a-ndvi-stats": []},
    lat_lim=[40.0, 41.0], lon_lim=[14.0, 15.0],
    path="out", resolution=10,
    api="batch-statistical",
    batch_output=batch_output,
)
tables = el.download()      # -> ['out/sentinel-2-l2a-ndvi-stats.csv']
```

!!! warning "Batch needs your own S3 bucket"
    The Batch and Batch-Statistical planes are **not** exercised in earthlens'
    live e2e tests because they require a user S3 bucket + IAM role. They are
    covered by faked-SDK integration tests and documented here; run them against
    your own bucket to validate end-to-end.
