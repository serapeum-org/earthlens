# Sampling and vector interop

Everything on this page answers a question about *places* rather than about a
whole raster: what is the value here, what is the average inside this polygon,
how does this pixel change over time, which polygons does this classification
imply. The raster side is covered in
[Usage](usage.md#7-client-side-raster-analysis) and the Earth-Engine side in
[Server-side computation](server-side-compute.md).

Two packages are involved:

- `earthlens.gee.sampling` — the `Sampler` family, one class per sampling
  pattern, each reading a raster over geometries.
- `earthlens.gee.vector` — the bridge between vector formats, `GeoDataFrame`s
  and Earth Engine objects, plus the raster-to-polygon direction.

## The `Sampler` family

Every sampler is a frozen value object holding its configuration, and every one
takes the same two things — a raster and the geometries to read it over:

```python
sampler.sample(image, gdf)         # -> ee.FeatureCollection
sampler.sample_to_gdf(image, gdf)  # -> pyramids FeatureCollection (where offered)
```

`scale_m` is required on all of them: a sample has no meaning without the
resolution it was taken at. `reducer` is resolved through the shared
[reducer registry](usage.md#5-the-object-api) **at construction**, so a typo or
a reducer that no sampling path can run fails before any request is made rather
than part-way through:

```python
from earthlens.gee.sampling import PointSampler

PointSampler(scale_m=30, reducer="mosaic")
# ValueError - 'mosaic' is not a reducer a sample/reduceRegions path can run
```

That strictness is deliberate and can be switched off with
`strict_reducers=False` when you know the reducer is valid for a path the check
does not model.

| Sampler | What it reads | Key options |
|---|---|---|
| `PointSampler` | the raster at point geometries | `reducer` (default `first`) |
| `RegionSampler` | per-polygon zonal statistics | `strategy`, `stats`, `tile_scale` |
| `RandomSampler` | `n_points` random labelled points in a region | `n_points`, `seed` |
| `StratifiedSampler` | `n_per_class` points per class | `class_band`, `n_per_class`, `class_values`, `seed` |
| `TransectSampler` | `n_points` samples along a line | `n_points`, `band` |
| `TimeSeriesSampler` | one geometry across a collection's time axis | `wide` |
| `ZonalHistogram` | per-polygon class histograms | `class_band`, `normalize`, `class_labels` |

### Points

`PointSampler` batches its work rather than issuing one request per point: the
points are loaded into an `rtree` index and one `reduceRegions` call is made per
index leaf, clipped to that leaf's bounding box. The number of round-trips
therefore tracks the leaf count, not the point count — which is what makes a
few thousand points practical.

```python
from earthlens.gee.sampling import PointSampler

values = PointSampler(scale_m=30).sample_to_gdf(image, stations_gdf)
```

The free functions `sample_points` / `sample_points_to_gdf` still exist and
behave identically — they are thin shims over `PointSampler`, which owns the one
copy of the batching loop. New code should prefer the class.

### Polygons

`RegionSampler` runs the zonal reduction either in Earth Engine or locally, and
which one is an explicit choice rather than a heuristic:

```python
from earthlens.gee.sampling import RegionSampler, ZonalStrategy

# Server-side: ee.Image.reduceRegions, the default.
stats = RegionSampler(
    scale_m=100, stats=("mean", "min", "max"), tile_scale=4
).sample_to_gdf(image, catchments_gdf)

# Client-side: reduce an already-downloaded pyramids Dataset locally.
local = RegionSampler(
    scale_m=100, strategy=ZonalStrategy.CLIENT
).sample_to_gdf(dataset, catchments_gdf)
```

`tile_scale` is the memory knob: raising it (2, 4, 8, 16) makes Earth Engine use
smaller tiles for the same reduction, which is the usual answer to a computation
that times out or runs out of memory on large or numerous polygons, at the cost
of more time.

`zonal_stats(image, gdf, scale_m=..., stats=..., strategy=...)` is the
function-shaped equivalent for a one-off call.

### Class areas, not class means

A zonal **mean** over a classification is meaningless — the average of class
codes 1, 2 and 5 is not a class. `ZonalHistogram` is the right reduction for a
categorical raster: it reports how much of each class falls inside each polygon.

```python
from earthlens.gee.sampling import ZonalHistogram

areas = ZonalHistogram(
    scale_m=10,
    class_band="Map",
    normalize="area",                       # or "count" (default) / "fraction"
    class_labels={10: "tree", 40: "crop", 80: "water"},
).sample_to_gdf(worldcover_image, admin_gdf)
```

`normalize` picks the unit: `count` gives pixel counts, `fraction` gives shares
that sum to 1 per polygon, and `area` gives real area — computed per-pixel
rather than from one nominal cell size, so it stays correct on a geographic CRS
where a pixel's ground area changes with latitude.

### Transects and time series

`TransectSampler` walks a `LineString`, returning `n_points` evenly spaced
samples along it — the usual input to a profile plot:

```python
from earthlens.gee.sampling import TransectSampler

profile = TransectSampler(scale_m=30, n_points=200).sample_to_gdf(dem_image, line_gdf)
```

`TimeSeriesSampler` holds the geometry still and moves along the collection's
time axis instead. It consumes an `ee.ImageCollection` rather than a single
image, and `wide=` chooses the shape of the answer — one row per date (long,
the default) or one column per date (wide):

```python
from earthlens.gee.sampling import TimeSeriesSampler

series = TimeSeriesSampler(scale_m=250, reducer="mean").sample_to_gdf(collection, plot_gdf)
```

### Training points

`RandomSampler` and `StratifiedSampler` draw labelled points for a supervised
workflow. The difference is where the balance comes from: random sampling
reflects whatever the landscape contains, so a rare class may get almost no
points; stratified sampling guarantees a quota per class.

```python
from earthlens.gee.sampling import RandomSampler, StratifiedSampler

anywhere = RandomSampler(scale_m=30, n_points=500, seed=42).sample(image, region_gdf)

balanced = StratifiedSampler(
    scale_m=30,
    class_band="landcover",
    n_per_class=200,                        # or {1: 300, 2: 150, 5: 50}
    class_values=(1, 2, 5),
    seed=42,
).sample(image, region_gdf)
```

Pass `seed=` whenever a result needs to be reproducible; without it Earth Engine
picks its own and two runs disagree.

## Vector interop

### Geometries into Earth Engine

`GeometryConverter` turns Shapely geometries, `GeoDataFrame`s and pyramids
`FeatureCollection`s into `ee.Geometry` / `ee.FeatureCollection`. It handles
`Polygon`, `Point` **and** `LineString` — the last of which the older free
`create_geometry` helper does not:

```python
from earthlens.gee.vector import GeometryConverter

converter = GeometryConverter(epsg=4326, columns=["name", "area_km2"])
region = converter.to_ee_geometry(shapely_polygon)
features = converter.to_ee(gdf)
```

`columns=` narrows what travels: only the named attributes are carried onto the
features, which keeps a wide `GeoDataFrame` from being shipped in full.

### Files in, files out

`VectorCodec` is the format edge — read a vector file (or a CSV of coordinates)
into Earth Engine, and write a collection back out:

```python
from earthlens.gee.vector import VectorCodec

codec = VectorCodec()
features = codec.from_file("basins.geojson")
points = codec.from_csv("stations.csv", x="lon", y="lat")
codec.to_file(features, "out/basins_sampled.geojson")
```

Its converter and downloader are injectable, which is how the Earth-Engine and
download seams stay shared rather than re-implemented per call site.

### Pulling features back

`FeatureDownloader` is the retry-and-concurrency budget for getting features out
of Earth Engine, rather than a bare `getInfo`:

```python
from earthlens.gee.vector import FeatureDownloader

downloader = FeatureDownloader(pool_size=25, tries=5, backoff=2.0)
frame = downloader.to_dataframe(features)
gdf = downloader.to_gdf(features)
combined = downloader.to_dataframes([a, b, c])  # several collections, one frame
```

`tries` and `backoff` give exponential retry on the transient failures a large
pull reliably produces; `pool_size` bounds the parallelism.

### Rasters to polygons

`Vectorize` is the raster-to-vector direction — `reduceToVectors` server-side,
or a local reduction over an in-memory pyramids `Dataset`:

```python
from earthlens.gee.vector import Vectorize

vec = Vectorize(scale_m=30, geometry_type="polygon", eight_connected=True)

polygons = vec.to_gdf(classified_image)    # server-side, pulled back
graph = vec.to_ee(classified_image)        # server-side, left as ee
local = vec.to_polygons(dataset)           # client-side, from a Dataset
```

`eight_connected` decides whether diagonal neighbours belong to the same polygon
(the default) or not, and `label_property` names the attribute the class value
lands in (`"label"` by default).

**Sieve first.** Vectorising an un-cleaned classification produces thousands of
single-pixel polygons, because every isolated misclassified pixel becomes its
own feature. Pair it with `Sieve` from the
[raster analysis ops](usage.md#patches-labelling-and-sieving):

```python
from earthlens.gee import Sieve

cleaned = gee.analyze(Sieve(min_patch_cells=25))
polygons = Vectorize(scale_m=30).to_polygons(cleaned)
```

**Mind the synchronous ceiling.** A `to_gdf` pull is bounded by Earth Engine's
`getInfo` limit of about 5000 features, published as
`earthlens.gee.vector.SYNC_FETCH_LIMIT`. A vectorisation expected to exceed it
should be routed through a
[`TableSink`](server-side-compute.md#table-exports-bigquery-and-featureview-options)
— a queued batch export — rather than a synchronous fetch. That routing is a
decision you make, not something the code does behind you, so size the result
before pulling it.

### Arrays and NetCDF

Two functions close the loop back to raster containers, both delegating to
pyramids:

```python
from earthlens.gee.vector import to_netcdf, to_numpy

array = to_numpy(dataset, band=0)           # one band as a numpy array
path = to_netcdf(dataset_collection, "out/stack.nc")
```

## See also

- [Usage](usage.md) — downloading, the object API, and client-side raster
  analysis.
- [Server-side computation](server-side-compute.md) — masks, filters, temporal
  helpers, and the batch export sinks including `TableSink`.
