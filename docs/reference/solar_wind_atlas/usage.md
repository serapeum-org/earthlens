# Solar & Wind Atlas — usage

The `solar-wind-atlas` backend downloads Global Solar Atlas and Global Wind
Atlas climatology layers for a bounding box and writes one GeoTIFF per layer. It
needs no credentials (both atlases are CC-BY-4.0). See
[Available layers](datasets.md) for the `variables=` ids and
[Introduction](introduction.md) for how the two transports work.

## A wind + solar subset

```python
from earthlens.core import EarthLens

paths = EarthLens(
    data_source="solar-wind-atlas",
    variables=["wind_100m", "ghi"],
    lat_lim=[55.0, 55.5],     # [south, north]
    lon_lim=[12.0, 12.5],     # [west, east], -180..180
    path="atlas_out",
).download()

paths   # [Path('atlas_out/wind_100m.tif'), Path('atlas_out/ghi.tif')]
```

`download()` returns the list of written GeoTIFF paths, one per requested layer,
named `<layer id>.tif` under `path`.

Read a result back with pyramids:

```python
from pyramids.dataset import Dataset

wind = Dataset.read_file("atlas_out/wind_100m.tif")
wind.epsg                  # 4326 (WGS84)
array = wind.read_array()  # mean wind speed at 100 m, in m/s
```

## Wind is windowed; solar downloads once

The two atlases use different transports (see
[Introduction](introduction.md)):

- **Wind layers** (`wind_100m`, `weibull_k_*`, `capacity_factor_iec*`,
  `air_density_100m`) are read **windowed** straight from the remote COG — a
  small bbox returns in seconds and transfers only the AOI.

- **Solar layers** (`ghi`, `dni`, `dif`, `gti`, `pvout`, `opta`) require a
  **one-time ~2.7 GB download** of the full global archive into a cache, after
  which the bbox is cropped locally. The first solar request logs a warning to
  that effect; later requests reuse the cache. Point the cache anywhere with
  `cache_dir=`:

```python
EarthLens(
    data_source="solar-wind-atlas",
    variables=["ghi"],
    lat_lim=[55.0, 55.5],
    lon_lim=[12.0, 12.5],
    path="atlas_out",
    cache_dir="/data/gsa_cache",   # default: <cache_dir()>/solar_wind_atlas
).download()
```

## Picking layers and aliases

`variables=` takes a list of layer ids; an unknown id raises a `ValueError`
with a did-you-mean hint. Four friendly aliases route to the same backend:

```python
# All equivalent entry points:
EarthLens(data_source="solar-wind-atlas", variables=["wind_100m"], ...)
EarthLens(data_source="global-wind-atlas", variables=["wind_100m"], ...)
EarthLens(data_source="gwa", variables=["wind_100m"], ...)
EarthLens(data_source="gsa", variables=["ghi"], ...)
```

## No temporal aggregation

Each layer is a single static climatology grid with no time axis, so there is
nothing to reduce over time. Passing `aggregate=` raises `NotImplementedError`:

```python
from earthlens.aggregate import AggregationConfig

EarthLens(
    data_source="solar-wind-atlas",
    variables=["ghi"],
    lat_lim=[55.0, 55.5],
    lon_lim=[12.0, 12.5],
).download(aggregate=AggregationConfig(freq="YS", op="mean"))
# NotImplementedError: ... static long-term-average climatology ... no temporal axis ...
```
