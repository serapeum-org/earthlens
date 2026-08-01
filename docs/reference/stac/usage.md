# STAC backend — usage

## Request shape

A request is a `variables` mapping of `{collection_key: [asset, ...]}` plus a
bbox and a date window:

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="earth-search",          # endpoint alias (pre-binds endpoint=)
    start="2024-06-01",
    end="2024-06-20",
    variables={"sentinel-2-l2a": ["red", "green", "blue"]},
    lat_lim=[40.40, 40.45],
    lon_lim=[-3.72, -3.67],
    path="out/madrid",
)
paths = el.download()                     # -> list of written COG paths
```

* The **collection key** is logical; the catalog resolves it to the id the
  endpoint actually serves (Earth Search calls Sentinel-2 L2A
  `sentinel-2-c1-l2a`).
* The **asset keys** are the names that endpoint serves — these differ across
  providers (Earth Search exposes `red`/`green`/`nir`; MPC exposes
  `B04`/`B08`). Pass an empty list to fall back to the collection's
  `default_assets`.

## Choosing an endpoint

Either open the facade with an **endpoint alias** (`"planetary-computer"`,
`"earth-search"`, `"cdse"`), which pre-binds `endpoint=`, or use
`data_source="stac"` and pass `endpoint=` yourself:

```python
el = EarthLens(data_source="stac", endpoint="planetary-computer",
               variables={"sentinel-2-l2a": ["B04", "B08"]}, ...)
```

With no endpoint the backend infers it from the first collection's home
endpoint.

## Optional knobs

`resolution=` (output GSD, metres), `epsg=` (output CRS), and `max_items=` (cap
the items per collection — handy for smoke pulls) are accepted as keyword
arguments and forwarded through the facade.

## Output

One COG per `(collection, acquisition date)` is written to `path`, named
`<collection>_<YYYY-MM-DD>.tif`. Multiple tiles covering the bbox on the same
date are mosaicked; multiple assets are stacked into the bands of one COG;
mismatched-CRS tiles (e.g. a multi-UTM-zone Sentinel-2 AOI) are reprojected to a
common CRS first.

### Antimeridian

An AOI that crosses 180° is expressed with `lon_lim=[west, east]` where
`west > east` (e.g. `lon_lim=[170, -170]`). It is split into an eastern and a
western half, and each date yields one COG per half
(`<collection>_<date>_part0.tif` east, `_part1.tif` west).

## Aggregation (`aggregate=`)

Because the output is raster, `download(aggregate=...)` is **supported**: a
multi-date pull is reduced per time window into per-window COGs.

```python
from earthlens.aggregate import AggregationConfig

el = EarthLens(data_source="earth-search",
               start="2024-01-01", end="2024-12-31",
               variables={"sentinel-2-l2a": ["red"]},
               lat_lim=[40.40, 40.45], lon_lim=[-3.72, -3.67], path="out/ndvi")
windows = el.download(aggregate=AggregationConfig(freq="1MS", op="mean",
                                                  out_dir="out/ndvi/monthly"))
```

Each acquisition date is labelled with its `freq` window; the per-date COGs are
reduced with `op` (`"auto"` → mean) via pyramids'
`DatasetCollection.groupby(labels).<op>()`, writing one COG per
`(collection, window)` named `<collection>_<op>_<freq>_<YYYYMMDD>.tif`.

## See also

* [Introduction](introduction.md) · [Authentication](authentication.md) ·
  [API reference](stac.md)
