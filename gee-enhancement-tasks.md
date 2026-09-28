# GEE backend enhancement — implementation plan & tasks

Companion to [`gee-missing-functionalities.md`](./gee-missing-functionalities.md).
This turns each Earth Engine capability the earthlens GEE backend does **not**
wrap into a self-contained, pick-up-ready task: a clear objective, where it
should live (`pyramids-eo` vs. earthlens directly), concrete implementation
details, the codebase context an agent needs, and acceptance criteria.

---

## Shared context (read first — applies to every task)

**Repo layout**

- Backend source: `libs/providers/imagery/src/earthlens/gee/`
  - `backend.py` — the `GEE(LazyClientMixin, AbstractDataSource)` class (~3k lines).
    Public: `catalog` property, `authenticate()`, `download()`, `client` (the raw
    initialised `ee` module — the escape hatch).
  - `catalog.py` + `catalog/*.yaml` — offline dataset/band metadata (1104 datasets).
  - `sampling.py` — `sample_points` / `sample_points_to_gdf` (**mirror this file's
    shape for any new point/zonal helper**).
  - `filters.py` — collection filters (`by_year`, `by_bounds`, …).
  - `cloud_masks.py` — per-image masks (`landsat_sr`, `sentinel2_scl`).
  - `io.py` — FeatureCollection → pandas/GeoPandas.
  - `features.py` — shapely/GeoDataFrame → `ee` geometry.
  - `jobs.py` — async task tracking (`TaskInfo`, `list_recent_tasks`, …).
  - `_eedai.py` — lazy guarded access to the `pyramids-eo` reader
    (`import_earthengine_reader()`, `eedai_available()`, `credentials_for()`).
  - `__init__.py` — the public re-export surface + `__all__` (**every new public
    symbol must be added here and documented in the module docstring**).
- Tests: `libs/providers/imagery/tests/gee/` — one `test_<module>.py` per module,
  plus `conftest.py`. **Add/extend the matching test file for every task.**
- Docs: `docs/reference/gee/` (`usage.md`, `server-side-compute.md`, …) and
  example notebooks in `docs/examples/gee/`.

**Dependencies (pinned)**: `earthengine-api==1.7.35`, `pyramids-eo==0.8.1`
(the `[eedai]` extra), `pyramids-gis==0.65.0` (base, imported as `pyramids`).

**The one architectural rule that decides "where":**
The EEDAI/EEDA GDAL driver **reads stored pixels — no server-side compute.**
- Work that runs *after* pixels are downloaded → implement **client-side** on a
  `pyramids.dataset.Dataset` (many methods already exist; just wire them up).
- Work that must run over the archive server-side (graphs, joins, ML training at
  scale, tiles, exports, asset ops) → a **thin earthlens wrapper over raw `ee`**,
  or leave it to the `.client` escape hatch and document it.

**`pyramids.dataset.Dataset` methods you'll reuse** (all verified present in
0.65.0): `slope`, `aspect`, `hillshade`, `apply`, `combine`, `where`,
`zonal_stats`, `stats`, `to_polygons`, `to_feature_collection`, `from_features`,
`cluster`, `sample`, `extract`, `proximity`, `focal_apply`, `plot`, `preview`,
`to_image`, `to_cog`, `convert_units`. `pyramids-eo` adds
`earthengine.estimate_earthengine_cost`/`ReadCost`,
`earthengine.collection_from_earthengine`, `sentinel.scl_mask`/`SclClass`,
`enhance.stretch`, `composites.true_color`, `resample.to_area`.

**Conventions to follow** (seen across the backend): module-level pure functions
for helpers (mirror `sampling.py`); `ValueError` with a "did-you-mean"/allowed
list on bad input; catalog-validate every band before building a graph; redact
credentials; keep new deps behind the existing `[eedai]` guard via
`import_earthengine_reader()` / `eedai_available()`; add a `gee`-marked test.

**Priority tiers**
- **Tier A** — ready-made `pyramids` method, wire-up only (highest value/effort ratio).
- **Tier B** — client-side, needs modest new earthlens code on top of `pyramids`.
- **Tier C** — server-side EE; thin `ee` wrapper in earthlens, or escape-hatch + docs.

---

# TIER A — wire up existing `pyramids` capability (client-side)

## Task A1 — Polygon zonal statistics
- **Objective:** given a downloaded raster and a polygon `GeoDataFrame`, return
  per-polygon statistics (mean/min/max/median/std/count/percentiles) as a
  `GeoDataFrame` — the polygon analogue of the existing point `sample_points`.
- **Where:** earthlens (`sampling.py`), delegating to `pyramids` `Dataset.zonal_stats`.
- **Implementation details:**
  - Add `zonal_stats(dataset, gdf, *, stats=("mean",), band=0)` and
    `zonal_stats_to_gdf(...)`. Accept either a `pyramids.dataset.Dataset` or a
    path to a downloaded GeoTIFF (open with `Dataset.read_file`).
  - Convert the polygon `GeoDataFrame` to a `pyramids.feature.FeatureCollection`
    (check `FeatureCollection.from_geodataframe`/`read_file`), then call
    `dataset.zonal_stats(fc, stats=stats, band=band)` (signature verified:
    `zonal_stats(self, fc, *, stats=('mean',), method='rasterize', band=0)`).
  - Validate `stats` names against the reducers pyramids supports; raise
    `ValueError` listing allowed names (mirror `_REDUCER_WHITELIST` in `sampling.py`).
- **Context/gotchas:** this operates on **downloaded** pixels, so it pairs with a
  prior `GEE.download()`. Document that archive-scale zonal stats over huge AOIs
  should stay on server-side `ee.Image.reduceRegions` via `.client`.
- **Public surface:** export both from `__init__.py`, document in docstring + `usage.md`.
- **Tests:** extend `test_sampling.py` — a small synthetic raster + 2 polygons,
  assert stat values; assert `ValueError` on an unknown stat.
- **Acceptance:** `zonal_stats_to_gdf(ds, polys, stats=("mean","max"))` returns one
  row per polygon with correct values; unknown stat raises.

## Task A2 — Terrain analysis (slope / aspect / hillshade)
- **Objective:** derive slope, aspect, and hillshade from a downloaded DEM
  (e.g. `USGS/SRTMGL1_003`, `COPERNICUS/DEM/GLO30`) with no extra EE round-trip.
- **Where:** earthlens — new module `terrain.py`, delegating to `pyramids`
  `Dataset.slope` / `aspect` / `hillshade` (verified signatures:
  `slope(*, band=0, units="degrees")`, `aspect(*, band=0)`,
  `hillshade(*, azimuth=315.0, altitude=45.0, band=0)`).
- **Implementation details:** functions `slope(dataset, *, units="degrees")`,
  `aspect(dataset)`, `hillshade(dataset, *, azimuth=315, altitude=45)` returning a
  new single-band `Dataset` (wrap the returned array back into a `Dataset` via
  `Dataset.dataset_like`/`from_array` so callers get a georeferenced result, not a
  bare ndarray). Accept a `Dataset` or a GeoTIFF path.
- **Context:** purely client-side; no `ee`. Good first showcase of "download once,
  analyse locally". Note EE's own `ee.Terrain` exists but needs a server round-trip.
- **Tests:** new `test_terrain.py` — synthetic elevation ramp → known slope; assert
  hillshade shape/dtype.
- **Acceptance:** slope of a known ramp matches expected within tolerance; outputs
  are georeferenced `Dataset`s.

## Task A3 — Quick-look thumbnails / PNG previews
- **Objective:** render a downloaded raster (single-band colormapped or 3-band RGB)
  to a PNG for quick visual QA, without opening a GIS.
- **Where:** earthlens — new module `preview.py`, delegating to `pyramids`
  `Dataset.plot` / `preview` / `to_image` and `pyramids_eo.enhance.stretch` +
  `pyramids_eo.composites.true_color`.
- **Implementation details:**
  - `quicklook(dataset, out_path, *, bands=None, stretch="linear", cmap=None)`.
  - RGB path: pull bands via `read_array`, run `enhance.stretch(img, kind=stretch,
    gamma=...)` → uint8, save PNG (via `Dataset.to_image` or PIL).
  - Single-band path: `Dataset.plot(band=..., color=...)` or a colormapped `to_image`.
  - Optionally a server-side alternative in a docstring note: `image.getThumbURL`
    for a preview *before* download (Tier C companion — see C2).
- **Context:** `composites.true_color(red, blue, nir, green=..., gamma=...)` exists
  for optical; `enhance.stretch(image, kind, min_stretch, max_stretch, gamma,
  cutoffs, dtype)` for display scaling.
- **Tests:** new `test_preview.py` — synthetic 3-band → PNG file exists, non-empty,
  correct size.
- **Acceptance:** `quicklook(ds, "out.png", bands=["B4","B3","B2"])` writes a valid
  PNG; single-band colormapped path works.

## Task A4 — Pre-download cost estimate
- **Objective:** let a caller see what a read will cost (scene count, total bytes,
  pixel dimensions) **before** committing to a download — for sizing/guarding.
- **Where:** earthlens — new `GEE.estimate_cost()` method, delegating to
  `pyramids_eo.earthengine.estimate_earthengine_cost`.
- **Implementation details:**
  - `estimate_earthengine_cost(asset_id, *, start, end, bbox=None, geometry=None,
    crs="EPSG:4326", credentials=..., property_filter=None) -> ReadCost`.
  - `ReadCost` fields (verified): `scene_count, total_size_bytes, max_width,
    max_height, max_band_count, min_pixel_size, scenes`.
  - Add `GEE.estimate_cost()` that reads the instance's request (asset(s), dates,
    bbox/region, crs, property_filter) and returns a `ReadCost` per asset. Reuse
    `self._eedai_credentials()` (`credentials_for`) for auth; guard with
    `import_earthengine_reader()` so it raises the friendly `[eedai]` message if
    the extra is missing.
  - Consider using it internally to enrich the existing 32768-px `ValueError`.
- **Context:** `_eedai.py` already has the credential adapter and import guard —
  reuse them. This is EEDA metadata only, no pixel fetch.
- **Tests:** extend `test_eedai.py` — monkeypatch `estimate_earthengine_cost`,
  assert `GEE.estimate_cost()` maps request fields correctly and surfaces `ReadCost`.
- **Acceptance:** `GEE(...).authenticate(...).estimate_cost()` returns `ReadCost`(s);
  missing `[eedai]` → the standard friendly `ImportError`.

## Task A5 — Sentinel-2 SCL cloud mask on downloaded data
- **Objective:** offer an SCL-based cloud mask that runs **client-side** on a
  downloaded S2 raster (complementing the existing server-side `sentinel2_scl`).
- **Where:** earthlens — extend `cloud_masks.py`, delegating to
  `pyramids_eo.sentinel.scl_mask` / `SclClass`.
- **Implementation details:** `scl_mask_local(dataset, classes=(...))` where
  `classes` accepts `SclClass`/str/int (signature verified:
  `scl_mask(dataset, classes, *, scl=None)`). Default to the same classes the
  server-side mask drops (shadow 3, cloud 8/9, cirrus 10). Requires the SCL band
  present in the download.
- **Context:** clearly distinguish from the existing `sentinel2_scl(image)` (an
  `ee.Image->ee.Image` hook applied *before* the composite server-side). This one
  is for data already on disk.
- **Tests:** extend `test_cloud_masks.py` — synthetic SCL band → masked pixels.
- **Acceptance:** pixels of the given SCL classes are masked in the returned Dataset.

---

# TIER B — client-side, new earthlens code over `pyramids`

## Task B1 — Band math / spectral indices (NDVI, NDWI, …)
- **Objective:** compute normalized-difference and arbitrary band-expression
  indices from a downloaded multi-band raster.
- **Where:** earthlens — new module `indices.py` over `pyramids` `Dataset.combine`
  / `apply` / `where`.
- **Implementation details:**
  - `normalized_difference(dataset, band_a, band_b) -> Dataset` via
    `Dataset.combine(other, func=lambda a,b: (a-b)/(a+b))` (or read two bands and
    compute). Provide named helpers `ndvi(ds, nir, red)`, `ndwi(ds, green, nir)`.
  - `band_expression(dataset, expr, bands)` — a safe evaluator over named band
    arrays (NO `eval`; use a small AST allow-list or numexpr).
  - Handle scale/offset: the catalog records `Band.scale`/`offset` but **nothing
    applies it** — expose an optional `apply_scale=True` that multiplies raw DN
    first (document the DN caveat from `server-side-compute.md`).
- **Context:** the server-side alternative is a `cloud_mask` hook computing
  `image.normalizedDifference([...])` and overwriting a declared band — cross-link
  both approaches in docs.
- **Tests:** new `test_indices.py` — synthetic NIR/Red → known NDVI; expression
  rejects unsafe input.
- **Acceptance:** `ndvi(ds, "B8", "B4")` returns a Dataset in [-1,1]; unsafe
  expression raises.

## Task B2 — Raster ↔ vector conversion
- **Objective:** vectorize a downloaded raster (e.g. a classified map → polygons)
  and rasterize a vector onto a raster grid.
- **Where:** earthlens — new module `vectorize.py` over `pyramids`
  `Dataset.to_polygons` / `to_feature_collection` / `from_features`.
- **Implementation details:** `to_polygons(dataset, *, band=0) -> GeoDataFrame`
  (via `to_feature_collection` then to GDF); `rasterize(gdf, *, template=dataset,
  column=...) -> Dataset` via `Dataset.from_features(fc, template=..., column_name=...)`.
- **Context:** server-side equivalents are `ee.Image.reduceToVectors` /
  `FeatureCollection.reduceToImage`; note them for archive-scale cases.
- **Tests:** new `test_vectorize.py` — round-trip a small labelled raster ↔ polygons.
- **Acceptance:** polygon count/attributes match the labelled regions; rasterize
  reproduces the mask on the template grid.

## Task B3 — Unsupervised clustering (k-means-style)
- **Objective:** segment a downloaded multi-band raster into k clusters locally.
- **Where:** earthlens — `classify.py`, over `pyramids` `Dataset.cluster`.
- **Implementation details:** `cluster(dataset, *, n_clusters=..., bands=None) ->
  Dataset`. Inspect `Dataset.cluster`'s real params first (it's a facade to
  `Vectorize.cluster`); if it targets polygonisation rather than pixel k-means,
  fall back to running `sklearn.cluster.KMeans` on `read_array` and wrapping the
  label array via `dataset_like`.
- **Context:** client-side analogue of `ee.Clusterer` (which trains server-side).
  State the scale caveat.
- **Tests:** new `test_classify.py` — synthetic 2-cluster image → 2 labels.
- **Acceptance:** returns a single-band label Dataset with `n_clusters` distinct values.

## Task B4 — Supervised classification (train locally, apply locally)
- **Objective:** extract training samples from a downloaded raster at labelled
  points, train a scikit-learn classifier, and apply it to the raster.
- **Where:** earthlens — extend `classify.py`; sampling via `pyramids`
  `Dataset.sample`/`extract`, apply via `Dataset.apply`.
- **Implementation details:**
  - `extract_training(dataset, points_gdf, label_col) -> (X, y)` using
    `Dataset.extract`/`sample` at point locations.
  - `classify(dataset, model) -> Dataset` — run a fitted sklearn estimator over
    `read_array` reshaped to `(pixels, bands)`, wrap predictions back to a Dataset.
  - Keep sklearn an **optional** dependency behind a new extra or a lazy import
    with a friendly error (mirror `_eedai.py`'s import guard).
- **Context:** ⚠️ partial vs. EE — training is local, so it won't scale like
  server-side `ee.Classifier.train`. Document clearly; recommend `.client` +
  `ee.Classifier` for continental training.
- **Tests:** extend `test_classify.py` — tiny separable dataset trains + predicts.
- **Acceptance:** end-to-end extract→train→classify on a synthetic set yields the
  expected labels.

## Task B5 — Focal / neighbourhood / texture operations
- **Objective:** smoothing, focal statistics, and GLCM-style texture on downloaded
  rasters (e.g. speckle reduction for SAR — the documented Sentinel-1 gap).
- **Where:** earthlens — `focal.py` over `pyramids` `focal_apply`/`focal_mean`/`focal_std`.
- **Implementation details:** `focal_mean(dataset, radius=1)`, `focal_std(...)`,
  `focal_apply(dataset, func, radius)` returning Datasets. For SAR speckle, provide
  `focal_median` via `focal_apply(np.median, ...)`.
- **Context:** directly addresses the "No Sentinel-1 preprocessing / speckle filter"
  gotcha in `server-side-compute.md`.
- **Tests:** new `test_focal.py` — a spike image smoothed by focal_mean.
- **Acceptance:** focal_mean reduces a single-pixel spike as expected.

## Task B6 — Proximity / distance rasters
- **Objective:** distance-to-feature rasters (e.g. distance to water/roads) from a
  downloaded mask.
- **Where:** earthlens — small helper in `focal.py` (or `analysis.py`) over
  `pyramids` `Dataset.proximity`.
- **Implementation details:** `proximity(dataset, *, target_values=..., units=...)`
  wrapping `Dataset.proximity` (check its exact params — it's a facade to
  `Analysis.proximity`).
- **Context:** server-side analogue is `ee.Image.distance`/`cumulativeCost`.
- **Tests:** extend `test_focal.py` / new test — a single target pixel → radial distances.
- **Acceptance:** distance grid is 0 at target and increases outward.

## Task B7 — Extended reducers & DatasetCollection time-series stats
- **Objective:** percentiles and trend/regression reducers over a **time series**
  of downloaded rasters, plus expose the multi-scene read.
- **Where:** earthlens — extend the EEDAI path to return
  `pyramids_eo.earthengine.collection_from_earthengine`'s `DatasetCollection`, then
  add numpy-based reducers.
- **Implementation details:**
  - Add an option to `GEE`/a helper to return the `DatasetCollection` instead of a
    per-bucket reduced Dataset (`collection_from_earthengine(asset_id, start=, end=,
    window=, bands=, property_filter=, credentials=)`).
  - Reducers: `percentile(collection, q)`, `linear_trend(collection)` (per-pixel
    slope over time via `numpy.polyfit`/vectorised least squares), `stats(...)`.
- **Context:** the existing backend reduces per bucket with a single named reducer;
  this adds the richer reducer family the catalog can't express. Watch memory —
  `DatasetCollection` holds scenes; document the AOI-size caveat and reuse the
  EEDAI scene/pixel budget constants for guardrails.
- **Tests:** extend `test_eedai.py` — a small synthetic collection → correct
  percentile/trend.
- **Acceptance:** `percentile(coll, 90)` and `linear_trend(coll)` return correct
  per-pixel Datasets on a synthetic stack.

---

# TIER C — server-side EE; thin `ee` wrapper in earthlens or escape-hatch + docs

> These need Earth Engine's server (graphs / tiles / exports / asset ops).
> `pyramids-eo` **cannot** implement them (it would have to download the archive).
> Each task is a thin, well-typed convenience over `self.client` (raw `ee`),
> catalog-validated where it touches assets — or, where a wrapper adds little,
> an explicit "escape-hatch only" doc task with a worked example.

## Task C1 — Named algorithm composites (Landsat/Sentinel-2, CloudScore+, etc.)
- **Objective:** one-call access to common `ee.Algorithms` composites
  (`Landsat.simpleComposite`, `Sentinel2.CDI`, CloudScore+ masking, pansharpening).
- **Where:** earthlens — extend `cloud_masks.py`/new `algorithms.py` as
  `ee.Image->ee.Image` / `ee.ImageCollection->ee.Image` hooks usable via the
  existing `cloud_mask=`/`filters=` seams.
- **Implementation details:** wrap the raw calls (e.g.
  `ee.Algorithms.Landsat.simpleComposite(collection)`), expose as composable
  callables matching the `CloudMask`/`CollectionFilter` type aliases.
- **Context:** these run server-side; they slot into the existing pipeline
  (`filterDate→filterBounds→filters→cloud_mask→select`). No pyramids involvement.
- **Tests:** `test_cloud_masks.py`/new — assert the callable builds the expected
  `ee` graph (mock `ee`).
- **Acceptance:** a CloudScore+ mask composes into `GEE(cloud_mask=...)` and downloads.

## Task C2 — Server-side thumbnails / map tiles (`getThumbURL` / `getMapId`)
- **Objective:** a preview URL/PNG *before* download, and XYZ tile URLs for
  interactive maps (folium/geemap-style).
- **Where:** earthlens — new `visualize.py` over raw `ee`
  (`image.getThumbURL(vis)`, `ee.data.getMapId`/`getTileUrl`).
- **Implementation details:** `thumbnail_url(image, vis_params, dimensions=...)`,
  `tile_url(image, vis_params)`. Accept vis params (palette/min/max/gamma).
- **Context:** complements the *client-side* PNG in Task A3 — A3 previews a
  download, C2 previews the server image without downloading.
- **Tests:** new `test_visualize.py` — mock `ee`, assert URL construction.
- **Acceptance:** returns a valid thumbnail URL and a tile-URL template.

## Task C3 — Table / vector export sinks (`Export.table.*`)
- **Objective:** export `ee.FeatureCollection`s to Drive/GCS/Asset/BigQuery/
  FeatureView, mirroring the existing image export sinks.
- **Where:** earthlens — extend the export machinery in `backend.py` +
  `jobs.py` (reuse `TaskInfo`/tracking).
- **Implementation details:** add `Export.table.to{Drive,CloudStorage,Asset,
  BigQuery,FeatureView}` paths parallel to the current
  `Export.image.to{Drive,CloudStorage,Asset}`; return destination strings / `TaskInfo`.
- **Context:** the async plumbing (poll, `wait_for_task_id`, `resolve_destination`)
  already exists — this is a new sink type reusing it.
- **Tests:** extend `test_jobs.py`/`test_backend.py` — mock `ee.batch`, assert task
  creation + tracking.
- **Acceptance:** a table export queues, tracks, and resolves its destination.

## Task C4 — Video / timelapse export (`Export.video.*`, `getVideoThumbURL`)
- **Objective:** animated timelapses from an `ImageCollection`.
- **Where:** earthlens — extend export machinery (server-side only).
- **Implementation details:** wrap `ee.batch.Export.video.to{Drive,CloudStorage}`
  and `ImageCollection.getVideoThumbURL`; expose fps/dimensions/vis params.
- **Tests:** extend `test_jobs.py` — mock, assert task shape.
- **Acceptance:** a video export queues and tracks.

## Task C5 — Collection joins (`ee.Join`)
- **Objective:** spatial/attribute joins between collections (e.g. pair scenes to
  reference features).
- **Where:** earthlens — new `joins.py` returning `CollectionFilter`-compatible
  callables, or documented `.client` recipes.
- **Implementation details:** thin wrappers over `ee.Join.{inner,saveAll,saveBest,
  simple}` + `ee.Filter`. Assess whether a wrapper adds enough over raw `ee`; if
  not, ship a documented recipe instead.
- **Tests:** `test_filters.py`/new — mock `ee`, assert join graph.
- **Acceptance:** a join composes into the pipeline or is documented with a runnable
  example.

## Task C6 — Hosted-model inference (`ee.Model` / Vertex AI)
- **Objective:** run predictions from a Vertex AI / Cloud-hosted model over imagery.
- **Where:** earthlens — **escape-hatch + docs only** (niche, heavy setup).
- **Implementation details:** document `ee.Model.fromVertexAi(...).predictImage(...)`
  via `.client` with a worked example in `server-side-compute.md`.
- **Acceptance:** a documented, runnable `.client` recipe exists.

## Task C7 — Asset management & ingestion (`ee.data.*`)
- **Objective:** create/delete/copy assets, set ACL/IAM, and ingest user rasters/
  tables into Earth Engine.
- **Where:** earthlens — optional `assets.py` thin wrappers over `ee.data`
  (`createAsset`, `deleteAsset`, `setAssetAcl`, `startIngestion`, …), **or**
  escape-hatch + docs if out of scope.
- **Implementation details:** if wrapping, keep it minimal and clearly separated
  from the read-focused backend; reuse the auth/project resolution.
- **Context:** the backend already calls `ee.data.getAsset` for extent discovery —
  the auth plumbing is in place.
- **Acceptance:** either minimal wrappers with tests (mock `ee.data`), or a
  documented decision to leave it to `.client` with examples.

## Task C8 — Ops: workload tags, profiling, project config
- **Objective:** attribute EECU usage (billing) via workload tags, and surface
  profiling.
- **Where:** earthlens — small `GEE` options / context manager over
  `ee.data.workloadTagContext` / `setWorkloadTag`, `ee.profilePrinting`.
- **Implementation details:** an optional `workload_tag=` on `GEE(...)` that wraps
  downloads in `ee.data.workloadTagContext(tag)`; a `profile=True` debug flag.
- **Tests:** extend `test_backend.py` — mock, assert the tag context is entered.
- **Acceptance:** downloads run inside the workload-tag context when set.

## Task C9 — Modern pixel API (`computePixels` / `getPixels`) — evaluate
- **Objective:** decide whether to add `ee.data.computePixels`/`getPixels` as a
  third `engine` (NumPy-direct, sometimes faster than `getDownloadURL`).
- **Where:** earthlens — `backend.py` `engine=` machinery (spike/evaluate first).
- **Implementation details:** prototype a `computePixels` read; compare speed and
  correctness vs. the `getDownloadURL` and EEDAI paths; only productise if it beats
  both for some regime. Largely **moot** since EEDAI already gives a direct-read
  path — document the finding either way.
- **Acceptance:** a written comparison + a go/no-go decision recorded in this plan.

---

# Suggested sequencing

1. **Tier A first** (A1–A5): highest value, mostly wiring existing `pyramids`
   methods; establishes the "download → analyse locally" pattern and helper-module
   conventions (esp. A1 zonal stats and A4 cost estimate).
2. **Tier B** (B1–B7): build the client-side analysis toolkit on that foundation;
   B7 (DatasetCollection + reducers) unlocks time-series work.
3. **Tier C** (C1–C9): server-side wrappers as demand warrants; C1 (algorithm
   composites) and C3 (table exports) are the most broadly useful; C6/C7/C9 may
   stay escape-hatch + docs.

**Cross-cutting for every task:** add the public symbol(s) to `__init__.py`
`__all__` + module docstring; extend the matching `tests/gee/test_*.py` with a
`gee`-marked test; update `docs/reference/gee/usage.md` and/or
`server-side-compute.md`; keep any new heavy dependency behind an optional-extra
import guard mirroring `_eedai.py`.

---

*Plan derived from the verified gap analysis in `gee-missing-functionalities.md`
(`earthengine-api==1.7.35`, `pyramids-eo==0.8.1`, `pyramids-gis==0.65.0`). Every
"already exists" claim about `pyramids` was checked against the installed 0.65.0
API.*
