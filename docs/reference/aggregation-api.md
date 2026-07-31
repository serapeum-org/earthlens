# Aggregation API

The temporal aggregator: reduce a downloaded stack into windowed composites (daily mean, monthly sum, …). For the
guide with worked examples, see [Temporal aggregation](../aggregation.md).

```python
from earthlens.core import AggregationConfig, aggregate_netcdf
```

`aggregate=` is forwarded only when **both** conditions hold: the backend's `OUTPUT_KIND` is `raster` or `mixed`
— the shapes a gridded reduction is defined for — **and** the backend declares `SUPPORTS_AGGREGATE`. A `vector` /
`tabular` backend is refused because the aggregator has no meaning on `GeoDataFrame` / `DataFrame` rows; a raster
backend that has not wired the reducer is refused for that reason instead. Either way the refusal is a
`NotImplementedError` raised before the backend's `download` runs. See [Base contracts](base/contracts.md).

## `AggregationConfig`

::: earthlens.aggregate.AggregationConfig

## `aggregate_netcdf`

::: earthlens.aggregate.aggregate_netcdf

## `iter_aggregate_netcdf`

Streams one reduced window at a time instead of materialising the whole cube — this is what keeps memory bounded
on a long time series.

::: earthlens.aggregate.iter_aggregate_netcdf

## `AggregatedWindow`

::: earthlens.aggregate.AggregatedWindow

## Reduction helpers

Public since 0.12.0 (previously `_reduce` and `_window_groups`).

::: earthlens.aggregate.reduce_time_axis

::: earthlens.aggregate.window_groups
