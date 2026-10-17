"""Google Earth Engine backend.

Downloads imagery from Google Earth Engine. A request is a mapping of
`{asset_id: [band, ...], ...}` (the addressable units of an Earth Engine
dataset are *bands*, and one image carries many at once), plus a date
range, a bbox (or a `GeoDataFrame` region), a temporal-compositing
resolution (`"raw"` / `"daily"` / `"monthly"` / `"yearly"`), and an
output pixel `scale` in metres. Asset ids and band metadata are resolved
through `Catalog`, which loads the per-category YAMLs under
`src/earthlens/gee/catalog/` (the GEE analogue of the ECMWF
`cds_data_catalog.yaml`, but shaped by Earth Engine's own data model —
see that directory's `_index.yaml` header).

Public surface (re-exported from this package):

* `GEE` — the backend; instantiate with a date range, a bbox, a
  `{asset_id: [band, ...]}` request, an output `scale`, and credentials
  (`service_account` + `service_key`, or a registered `project`), then
  call `GEE.download`.
* `AuthenticationError` — raised when Earth Engine cannot be
  initialised (missing/invalid key, unregistered project, missing IAM role).
* `CloudMask` / `CollectionFilter` — the callable type
  aliases for the `GEE(cloud_mask=..., filters=...)` hooks
  (`ee.Image -> ee.Image` and `ee.ImageCollection -> ee.ImageCollection`);
  handy for annotating your own masks / filters.
* `Catalog` — pydantic-backed loader for the bundled per-category
  catalog under `src/earthlens/gee/catalog/`, exposing
  `available_datasets`, `datasets`, `providers`, and
  `get_dataset` / `get_band` / `get_variable` / `get_provider`.
* `Dataset` / `Band` / `Cadence` / `Extent`
  / `Provider` — the frozen value objects the catalog is built
  from.
* `CATALOG_PATH` / `PROVIDERS_PATH` — absolute paths to
  the bundled catalog directory and `providers.yaml`; monkey-patchable
  to redirect the loader at a temp directory.
* `EarthEngineAuth` — the low-level service-account auth helper
  (`ee.Initialize` against a registered project; base64 key encode/decode).
* `create_geometry` / `create_feature` — Shapely /
  `GeoDataFrame` → `ee.Geometry` / `ee.FeatureCollection` converters.
* `sample_points` / `sample_points_to_gdf` — sample a
  raster at point locations via leaf-batched `reduceRegions`; the
  `_to_gdf` variant returns a `GeoDataFrame` via `getInfo()`.
* `feature_collection_to_dataframe` /
  `feature_collections_to_dataframe` /
  `feature_collection_to_gdf` — download
  `ee.FeatureCollection`s to pandas / GeoPandas (sync + parallel +
  small-FC `getInfo()` paths).

Object surface (also re-exported) — the strategy objects, the frozen
value objects and the work-unit payloads the backend is built from.
`EnginePlan`, `ExportConfig`, `EngineConfig`, `OutputGrid`, `Artifact`
and `DownloadResult` are frozen pydantic models; the `WorkUnit` family
is a plain ABC and its subclasses, mutable by design. Two constructor
params,
`reducer=` and `engine=`, accept an instance in place of their string;
the rest of these are config, payload and return types rather than
anything a string names:

* `Reducer`, with `REDUCERS` and `resolve_reducer`
  — the reduction strategies. `GEE(reducer=...)` accepts a name, a spec
  string or an instance: `"mean"`, `Mean()`, `"percentile:90"` and
  `Percentile(q=90)` all work. The seven that reduce a time
  bucket's images are `Mean` / `Median` / `Min` /
  `Max` / `Sum` / `Mosaic` / `Mode`, which
  `reducers.collection_reducers()` names. `REDUCERS` is the whole
  registry, so it also holds the region-only `first` / `stdDev` /
  `variance` / `count` that `sample_points` uses and that
  `GEE(reducer=...)` refuses with `ValueError` ("reduces pixels in a
  region, not images in a collection"). `Percentile` is
  deliberately **not** in the registry — it needs a parameter — so it
  is reachable only as a spec string or an instance.
* `Engine`, with `ENGINES` and `resolve_engine` —
  what materialises the pixels. `engine="auto"` routes between
  `EedaiEngine` (the pyramids-eo reader) and `EeEngine`
  (`getDownloadURL`), falling back to the latter whenever the reader
  declines. `PixelsEngine` (`computePixels`, optionally against
  `HIGH_VOLUME_ENDPOINT`) is opt-in via `engine="pixels"` and is
  never selected by `auto`. `AutoEngine` is that routing
  decision as an object.
* `EnginePlan` (back-compat alias `EedaiPlan`) — the frozen
  routing verdict an engine returns, carrying the reason when a route
  is declined.
* `Sink`, with `SINKS` and `resolve_sink` — where
  the artifact goes: `UrlSink`, `DriveSink`,
  `GcsSink`, `AssetSink`. Unlike `reducer=` / `engine=`,
  `GEE(export_via=...)` takes the **name** only; pass an instance and
  `ExportConfig` rejects it. Call `resolve_sink` yourself
  when you want the object.
* `WorkUnit` / `RasterWorkUnit` — one time bucket's
  payload. `FeatureWorkUnit` is the vector/tabular seam for the
  later epics; nothing produces one yet.
* `Artifact` / `DownloadResult` — what `download()`
  returns under `materialize=True`: the in-memory pyramids object
  *alongside* the written file, iterating and indexing like the list of
  paths the method returns without it.
* `ExportConfig`, composed in `GEE.__init__`, and
  `EngineConfig`, derived on each read from the live attributes
  so a reassigned `crs` or `scale` cannot go stale behind it.
* `OutputGrid` — the one owner of the AOI-to-grid derivation
  (dimensions, affine transform, lattice snapping, tiling).
* `EedaiReader` — the adapter wrapping the optional pyramids-eo
  reader, owning its process-global credential hazard.

Client-side raster analysis (`earthlens.gee.analysis`, re-exported
here) runs *after* a download, on the in-memory pyramids `Dataset`, so
none of it reaches Earth Engine and none of it affects the
engine-eligibility gate:

* `RasterOperation` — the contract every operation implements,
  `Dataset -> Dataset`; `OperationChain` is the `>>` composition of
  several, itself an operation; `RasterAnalysis` adds knowing how to
  take the raster out of a `DownloadResult`, and is what
  `GEE.analyze` delegates to.
* `NormalizedDifference` with the presets `Ndvi` / `Ndwi` / `Ndbi`
  — two-band index arithmetic.
* `BandExpression` — the general case: a formula over named bands,
  compiled from an `ast` allow-list (`ALLOWED_FUNCTIONS`) with no
  `eval`, refused at construction rather than at run time.
* `Slope` / `Aspect` / `Hillshade` — terrain derivatives of an
  elevation band.
* `FocalMean` / `FocalStd` / `FocalMedian` / `FocalApply` —
  neighbourhood statistics over a square window; `Texture` with
  `MEASURES` is the grey-level co-occurrence family built on it.
* `Proximity` — distance from every cell to the nearest target cell.
* `Reclassify` / `ConvertUnits` — values into classes, or into
  another physical unit.
* `ConnectedComponents` / `Sieve` — labelling contiguous patches and
  dropping the ones too small to keep.
* `Cluster` / `Classify`, with `TrainingExtractor` and
  `TrainingSet` — unsupervised and supervised per-pixel learning
  through scikit-learn (the `[gee]` extra).

Two submodules ship more specialised helpers and are intentionally
**not** re-exported at this top level — import them directly. Both
feed the `GEE` backend's `cloud_mask=` / `filters=` constructor hooks
(`.map`-applied / composed before the reducer), so a cloud-masked
median composite is a facade call rather than raw `ee`:

* `earthlens.gee.cloud_masks` — per-image `ee.Image -> ee.Image`
  masks: `landsat_sr(image, sensor=...)` (Landsat C2-L2 QA_PIXEL
  Clear-bit) and `sentinel2_scl(image)` (Sentinel-2 L2A SCL —
  drops cloud shadow / cloud / cirrus).
* `earthlens.gee.filters` — `by_year` / `by_bounds` /
  `by_property_in` / `by_cloud_cover_lte` / `by_year_and_bounds`
  for `ee.ImageCollection.filter*` composition.

Job / task tracking — for asynchronous `export_via="drive"` /
`"gcs"` / `"asset"` sinks, the `earthlens.gee.jobs` module
exposes a small set of helpers re-exported from this package:

* Pass `wait_for_export=False` to `GEE(...)` so `download()` returns
  `TaskInfo` value objects at submission time instead of
  blocking until each task reaches a terminal state.
* `list_recent_tasks` (also as `Catalog.list_recent_tasks`)
  lists every Earth Engine batch task on the current project, with
  filters by `state` / age / `task_type` / `description_prefix`.
* `get_task_status` (also as `Catalog.get_task_status`),
  `cancel_task`, `wait_for_task_id` operate by task id —
  no live `ee.batch.Task` handle required, so they work across
  process / session boundaries.
* `resolve_destination` surfaces a completed task's
  destination URIs (Drive / GCS / asset paths) for follow-up
  pulls.

Mirrors `earthlens.ecmwf.jobs` — see also the "Tracking asynchronous
Earth Engine exports" notebook, `docs/examples/gee/track-batch-exports.ipynb`.

The Earth Engine SDK (`earthengine-api`, the `[gee]` extra) is imported
when this package is imported — install `earthlens[gee]` to use it; the
`EarthLens` facade still imports without it (it loads each backend
lazily). Authentication setup is documented under
`docs/reference/google-earth-engine/`.

Examples:
    - List datasets and look up a band's metadata (no network):

        ```python
        >>> from earthlens.gee import Catalog
        >>> cat = Catalog()
        >>> "USGS/SRTMGL1_003" in cat.datasets
        True
        >>> cat.get_dataset("USGS/SRTMGL1_003").title
        'NASA SRTM Digital Elevation 30m'
        >>> cat.get_band("UCSB-CHG/CHIRPS/DAILY", "precipitation").units
        'mm/d'

        ```
"""

from __future__ import annotations

from earthlens.gee.analysis import (
    ALLOWED_FUNCTIONS,
    MEASURES,
    Aspect,
    BandExpression,
    Classify,
    Cluster,
    ConnectedComponents,
    ConvertUnits,
    FocalApply,
    FocalMean,
    FocalMedian,
    FocalStd,
    Hillshade,
    Ndbi,
    Ndvi,
    Ndwi,
    NormalizedDifference,
    OperationChain,
    Proximity,
    RasterAnalysis,
    RasterOperation,
    Reclassify,
    Sieve,
    Slope,
    Texture,
    TrainingExtractor,
    TrainingSet,
)
from earthlens.gee.auth import AuthenticationError, EarthEngineAuth
from earthlens.gee.backend import GEE, CloudMask, CollectionFilter
from earthlens.gee.catalog import (
    CATALOG_PATH,
    PROVIDERS_PATH,
    Band,
    Cadence,
    Catalog,
    Dataset,
    Extent,
    Provider,
)
from earthlens.gee.configs import EngineConfig, ExportConfig
from earthlens.gee.engines import (
    ENGINES,
    AutoEngine,
    EedaiEngine,
    EeEngine,
    Engine,
    resolve_engine,
)
from earthlens.gee.features import create_feature, create_geometry
from earthlens.gee.grid import OutputGrid
from earthlens.gee.io import (
    feature_collection_to_dataframe,
    feature_collection_to_gdf,
    feature_collections_to_dataframe,
)
from earthlens.gee.jobs import (
    TaskInfo,
    cancel_task,
    get_task_status,
    list_recent_tasks,
    resolve_destination,
    wait_for_task_id,
)
from earthlens.gee.pixels import HIGH_VOLUME_ENDPOINT, PixelsEngine
from earthlens.gee.plans import EedaiPlan, EnginePlan
from earthlens.gee.reader import EedaiReader
from earthlens.gee.reducers import (
    REDUCERS,
    Max,
    Mean,
    Median,
    Min,
    Mode,
    Mosaic,
    Percentile,
    Reducer,
    Sum,
    resolve_reducer,
)
from earthlens.gee.sampling import sample_points, sample_points_to_gdf
from earthlens.gee.sinks import (
    SINKS,
    AssetSink,
    DriveSink,
    GcsSink,
    Sink,
    UrlSink,
    resolve_sink,
)
from earthlens.gee.work import (
    Artifact,
    DownloadResult,
    FeatureWorkUnit,
    RasterWorkUnit,
    WorkUnit,
)

__all__ = [
    "GEE",
    "AuthenticationError",
    "CloudMask",
    "CollectionFilter",
    "Catalog",
    "Dataset",
    "Band",
    "Cadence",
    "Extent",
    "Provider",
    "CATALOG_PATH",
    "PROVIDERS_PATH",
    "EarthEngineAuth",
    "create_geometry",
    "create_feature",
    "feature_collection_to_dataframe",
    "feature_collection_to_gdf",
    "feature_collections_to_dataframe",
    "sample_points",
    "sample_points_to_gdf",
    "TaskInfo",
    "list_recent_tasks",
    "get_task_status",
    "cancel_task",
    "wait_for_task_id",
    "resolve_destination",
    "Reducer",
    "Mean",
    "Median",
    "Min",
    "Max",
    "Mode",
    "Mosaic",
    "Sum",
    "Percentile",
    "REDUCERS",
    "resolve_reducer",
    "EnginePlan",
    "EedaiPlan",
    "OutputGrid",
    "ExportConfig",
    "EngineConfig",
    "Engine",
    "EeEngine",
    "EedaiEngine",
    "AutoEngine",
    "ENGINES",
    "resolve_engine",
    "Sink",
    "UrlSink",
    "DriveSink",
    "GcsSink",
    "AssetSink",
    "SINKS",
    "resolve_sink",
    "EedaiReader",
    "WorkUnit",
    "RasterWorkUnit",
    "FeatureWorkUnit",
    "Artifact",
    "DownloadResult",
    "PixelsEngine",
    "HIGH_VOLUME_ENDPOINT",
    "RasterOperation",
    "OperationChain",
    "RasterAnalysis",
    "NormalizedDifference",
    "Ndvi",
    "Ndwi",
    "Ndbi",
    "BandExpression",
    "ALLOWED_FUNCTIONS",
    "Slope",
    "Aspect",
    "Hillshade",
    "FocalMean",
    "FocalStd",
    "FocalMedian",
    "FocalApply",
    "Texture",
    "MEASURES",
    "Proximity",
    "Reclassify",
    "ConvertUnits",
    "ConnectedComponents",
    "Sieve",
    "Cluster",
    "Classify",
    "TrainingExtractor",
    "TrainingSet",
]
