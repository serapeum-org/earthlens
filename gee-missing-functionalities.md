# Earth Engine functionality not yet wrapped by earthlens

A gap analysis between the **`earthengine-api`** package (v1.7.35, the version
pinned in `uv.lock`) and the **earthlens GEE backend**
(`libs/providers/imagery/src/earthlens/gee/`).

## TL;DR

earthlens deliberately wraps a *narrow vertical slice* of Earth Engine:

> authenticate → build / filter / composite an `ImageCollection` → download
> analysis-ready GeoTIFFs, plus basic vector download and point sampling, plus
> batch-task tracking.

The `ee` SDK is a full planetary-scale **compute platform**. By rough count,
earthlens's convenience layer touches ~30 `ee` entry points, while `ee` exposes
**37 top-level classes, 78 `ee.data` functions, and 239 methods on `Image`
alone**.

Everything listed below as "missing" is still *reachable* through the raw
escape hatch — `el.datasource.client` returns the authenticated `ee` module
(see `docs/reference/gee/server-side-compute.md`). "Missing" means there is
**no earthlens convenience wrapper**: no offline catalog validation, no
pandas / GeoTIFF materialization, and no earthlens error types.

---

## What earthlens already covers (baseline)

| Area | Wrapped surface |
|---|---|
| Auth | `ee.Initialize`, `ee.ServiceAccountCredentials`, `ee.Authenticate` |
| Collection build | `ee.ImageCollection`, `.filterDate` / `.filterBounds` / `.filter` / `.map`, `.select`, temporal reduce (`mean`/`median`/`min`/`max`/`mode`/`mosaic`/`sum`) |
| Download | image `getDownloadURL`; batch `Export.image.to{Drive,CloudStorage,Asset}`; EEDAI direct read |
| Vectors | `create_geometry` / `create_feature` (shapely → ee), FC → DataFrame / GeoDataFrame, point sampling via `reduceRegions` |
| Filters / masks | 5 collection filters, 2 cloud masks (Landsat `QA_PIXEL`, S2 `SCL`) |
| Jobs | task tracking via `ee.data.listOperations` / `getOperation` / `cancelOperation` |

Everything below is present in `earthengine-api` but has **no earthlens
convenience wrapper**.

---

## 1. Machine learning & classification — entirely absent

- **`ee.Classifier`** (29 methods) — supervised classification / regression:
  `smileRandomForest`, `smileCart`, `smileGradientTreeBoost`, `libsvm`,
  `minimumDistance`, … with `.train()`, `.classify()`, `.explain()`,
  `.confusionMatrix()`.
- **`ee.Clusterer`** (18) — unsupervised: `wekaKMeans`, `wekaXMeans`,
  `wekaCascadeKMeans`, `wekaLVQ`, …
- **`ee.ConfusionMatrix`** — accuracy assessment (kappa, producer / consumer
  accuracy).
- **`ee.Model`** (15) — Vertex AI / Cloud ML hosting: `fromVertexAi`,
  `predictImage`, `predictProperties`.
- Image / FC hooks: `Image.classify`, `Image.cluster`,
  `FeatureCollection.errorMatrix`, `FeatureCollection.cluster`.

## 2. Raster algebra / band math / per-pixel transforms — not wrapped

`ee.Image` has **239 methods**; earthlens uses `select` + a temporal reduce.
Unwrapped:

- Arithmetic & logical ops; **`.expression()`** (arbitrary formulas);
  **`.normalizedDifference()`** (NDVI / NDWI / …).
- `.addBands` / `.rename` / `.clamp` / `.remap` / `.where`.
- `.clip` / `.mask` / `.updateMask` / `.unmask`.
- `.reproject` / `.resample` / `.reduceResolution`.
- Neighborhood / focal ops; `.convolve` (with `ee.Kernel`, 33 members);
  `.glcmTexture`; `.connectedComponents`; `.entropy`; `.cumulativeCost`;
  `.distance`; `.rgbToHsv`.

> You can inject any of these through the `cloud_mask=` per-image hook, but
> there is no earthlens helper, and you cannot *request* a band the hook
> creates — every requested band is catalog-validated first.

## 3. Reducers beyond the ~7 whitelisted

`ee.Reducer` has **74** members; earthlens exposes a handful by name. Missing
conveniences:

- `percentile`
- `linearFit` / `linearRegression` / `robustLinearRegression` / `sensSlope`
  (trend analysis)
- `histogram` / `frequencyHistogram` / `autoHistogram`
- `covariance` / `pearsonsCorrelation` / `kendallsCorrelation`
- `kurtosis` / `skew`
- reducer combinators `combine` / `group` / `repeat` / `setOutputs`

## 4. Zonal statistics & vector operations

- **Polygon zonal stats** — `Image.reduceRegion` / grouped `reduceRegions`
  over polygons. earthlens's `sample_points` only does **points**.
- **Raster ↔ vector** — `Image.reduceToVectors` (vectorize),
  `FeatureCollection.reduceToImage` (rasterize) — neither wrapped.
- **`ee.Join`** (18) — `inner` / `saveAll` / `saveBest` / `saveFirst` /
  `simple` spatial & attribute joins between collections.
- **Server-side FC ops** — `.aggregate_*`, `.reduceColumns`, `.randomColumn`,
  `.distance`, `.draw`, `.makeArray`, `.filter` / `.map`. earthlens only
  *downloads* FCs.
- **`Image.sample` / `sampleRegions` / `stratifiedSample`** — training-data
  extraction (used with section 1).

## 5. Server-side data structures & linear algebra

`ee.Array` (127), `ee.List` (51), `ee.Dictionary`, `ee.Number`, `ee.String`,
`ee.Date` / `ee.DateRange` — none wrapped. These power harmonic / Fourier
regression, PCA, matrix ops (`matrixMultiply`, eigen / SVD), and server-side
date arithmetic.

## 6. Terrain analysis

**`ee.Terrain`**: `slope`, `aspect`, `hillshade`, `hillShadow`, `fillMinima`,
`products` — no earthlens wrapper (SRTM / DEM datasets are in the catalog, but
the *analysis* is not).

## 7. Visualization / thumbnails / tiles / video — completely missing

earthlens downloads raw data; it never renders. Unwrapped:

- **Static previews** — `Image.getThumbURL` / `getThumbId`, `Image.visualize`
  (palette / min-max / gamma), `ee.data.getThumbnail`.
- **Interactive map tiles** — `Image.getMapId`, `ee.data.getTileUrl` /
  `TileFetcher` (the folium / geemap XYZ path).
- **Video / timelapse** — `ImageCollection.getVideoThumbURL` /
  `getFilmstripThumbURL`, `Export.video.to{Drive,CloudStorage}`.
- **Tile pyramids** — `Export.map.toCloudStorage`.

## 8. Export sinks earthlens does not expose

earthlens does **image** exports only. Missing:

- `Export.table.*` — `toDrive` / `toCloudStorage` / **`toBigQuery`** /
  **`toFeatureView`** / `toAsset`
- `Export.video.*`
- `Export.map.*`
- `Export.classifier.toAsset` (persist trained models)

## 9. Asset management & ingestion (`ee.data`) — none

- Lifecycle: `createAsset` / `createFolder` / `deleteAsset` / `copyAsset` /
  `renameAsset` / `updateAsset` / `setAssetProperties`.
- Listing: `listAssets` / `listImages` / `listFeatures`.
- ACL / IAM: `getAssetAcl` / `setAssetAcl` / `getIamPolicy` / `setIamPolicy`.
- Quotas: `getAssetRootQuota` / `getAssetRoots`.
- **Ingestion**: `startIngestion` / `startTableIngestion` /
  `startExternalImageIngestion` / `create_assets` (upload *your own* rasters /
  tables into EE).

earthlens only calls `getAsset` (for extent discovery).

## 10. Modern high-throughput pixel API — not used

`ee.data.getPixels` / `computePixels` / `computeImages` / `computeFeatures` /
`computeValue` / `getDownloadId` / `listImages` / `listFeatures` — the newer
Cloud API that returns NumPy arrays directly (often faster than
`getDownloadURL`). earthlens uses the older `getDownloadURL` + EEDAI. This
could be a natural **third engine**.

## 11. Platform / ops plumbing

- `ee.profilePrinting` & `ee.data.profiling` (EECU profiling).
- `Serializer` / `deserializer` (save / load computation graphs as JSON).
- `setDeadline` / `setMaxRetries`.
- **Workload tags** — `setWorkloadTag` / `workloadTagContext` (billing / quota
  attribution).
- `getProjectConfig` / `updateProjectConfig`.
- `ee.Algorithms` — the full named-algorithm registry: LandTrendr / CCDC
  temporal segmentation, SNIC segmentation, CloudScore+, pansharpening, Canny /
  Hough edge detection, `Landsat.simpleComposite`, etc.

---

## Bottom line

earthlens is a **data-acquisition facade**, not an Earth Engine analysis SDK.
The gaps cluster into five themes it intentionally leaves to raw `ee`:

1. **Compute / analysis** — band math, ML classification / clustering, terrain,
   zonal stats, joins, array / matrix, `ee.Algorithms`.
2. **Rendering** — thumbnails, map tiles, video, visualization params.
3. **Vector / table exports** — `Export.table.*`, BigQuery, FeatureView.
4. **Asset lifecycle** — create / delete / ACL / ingest your own assets.
5. **Ops** — profiling, workload tags, the modern `computePixels` API.

### Natural next additions (adjacent to earthlens's existing mission)

The gaps most in line with "download imagery + sample" — natural fits rather
than scope creep:

- **Polygon zonal statistics** — extends the existing `sample_points`.
- **Quick-look thumbnails** — `getThumbURL`.
- **Table / vector export sinks** — mirrors the existing image sinks.
- **More reducers exposed by name** — `percentile`, `linearFit` (trends).
- **Spectral-index / cloud-mask conveniences** — NDVI etc., alongside the
  existing two masks.

---

# Can these gaps be filled with `pyramids-eo` instead of raw `ee`?

earthlens already depends on **`pyramids-eo`** (0.8.1, the `[eedai]` extra) and
its base **`pyramids` / `pyramids-gis`** (0.65.0). The GEE backend uses the
GDAL **EEDAI** driver through `pyramids_eo.earthengine` for its fast read path.
The question: how many of the missing functionalities above can be implemented
on the `pyramids` side rather than requiring raw `ee`?

## The architectural line

The EEDAI / EEDA GDAL driver **reads stored pixels — it runs no server-side
compute.** So the gaps split cleanly:

- **Client-side, post-download** work on the `Dataset` / array you fetched →
  `pyramids-eo` / `pyramids` can do it (and often already has the method).
- **Server-side EE graph** work over the petabyte archive → needs raw `ee`;
  pyramids can't, because it would have to download the world first.

## What `pyramids-eo` (+ base `pyramids`) offers

`pyramids.dataset.Dataset` exposes **124 methods**. The ones relevant here:

- **Terrain:** `slope`, `aspect`, `hillshade`, `to_terrain_rgb`
- **Band math / analysis:** `apply`, `combine(other, func)`, `where`, `round`,
  `convert_units`, `astype`, `overlay`, `focal_apply` / `focal_mean` /
  `focal_std`, `map_blocks`
- **Statistics:** `stats`, `zonal_stats(fc, stats=..., band=...)`,
  `get_histogram`, `sample`, `extract`, `point`
- **Raster ↔ vector:** `to_polygons`, `to_feature_collection`, `contour`,
  `footprint`, `sieve`, `from_features` (rasterize), `from_points`
- **Unsupervised classification:** `cluster`
- **Warp / geometry:** `to_crs`, `resample`, `align`, `crop`, `clip`,
  `proximity`, `orthorectify`, `warped_view`
- **Visualization:** `plot`, `preview`, `to_image`, `plot_histogram`,
  `set_color_ramp`
- **COG / IO:** `to_cog`, `cog_info`, `validate_cog`, `create_overviews`,
  `from_wcs` / `from_wms` / `from_wmts` / `from_zarr`, `to_stac_item`

`pyramids-eo` submodules on top:

| Submodule | Offers |
|---|---|
| `earthengine` | EEDAI reader: `from_earthengine`, `collection_from_earthengine`, `EarthEngineCredentials`, **`estimate_earthengine_cost` / `ReadCost`**, `Window` |
| `sentinel` | `from_sentinel2`, `collection_from_sentinel2`, **`scl_mask` / `SclClass`**, `open_product` |
| `composites` | `true_color`, `night_ir`, solar/satellite geometry, `rayleigh_correct`, `sunz_correct` |
| `enhance` | `stretch` (physical values → display range / gamma / dtype) |
| `resample` | `to_area` (warp onto an exact CRS + extent + size grid) |
| `sensors` | geostationary calibration (FCI, SEVIRI) — not GEE-relevant |
| `stac` | STAC asset URL signers (Planetary Computer, CDSE, Earthdata, …) |

**What earthlens uses today:** only `from_earthengine`, `EarthEngineCredentials`,
plus base `Dataset.from_archive` / `from_bytes`, `merge_rasters`, and `to_cog`.
Everything else above is available but unused.

## Gap-by-gap verdict

| Missing functionality | pyramids-eo? | How |
|---|---|---|
| Terrain: slope / aspect / hillshade | ✅ already in pyramids | `Dataset.slope/aspect/hillshade` on a downloaded DEM |
| Polygon zonal statistics | ✅ already in pyramids | `Dataset.zonal_stats(fc, stats=...)` — polygon analogue of `sample_points` |
| Band math / NDVI / spectral indices | ✅ | `Dataset.apply` / `combine` / `where` |
| Raster ↔ vector (vectorize / rasterize) | ✅ already in pyramids | `to_polygons`, `to_feature_collection`, `from_features` |
| Thumbnails / RGB composites / visualization | ✅ | `enhance.stretch` + `composites.true_color` + `Dataset.plot`/`preview`/`to_image` |
| Unsupervised clustering (KMeans-like) | ✅ | `Dataset.cluster` — client-side analogue of `ee.Clusterer` |
| More reducers (percentile / stats / trends) | ⚠️ partial | `Dataset.stats` / `zonal_stats` + numpy across a `DatasetCollection`; AOI-scale, not archive-scale |
| Convolution / focal / texture | ✅ | `focal_apply` / `focal_mean` / `focal_std` |
| Proximity / distance | ✅ | `Dataset.proximity` |
| Extra cloud masks (SCL) | ✅ | `pyramids_eo.sentinel.scl_mask` / `SclClass` |
| COG output | ✅ already used | `Dataset.to_cog` |
| Pre-read cost estimate | ✅ available, not surfaced | `estimate_earthengine_cost` → `ReadCost(scene_count, total_size_bytes, max_width/height, …)` |
| Multi-scene collection read + `property_filter` | ✅ available, not surfaced | `collection_from_earthengine` → a `DatasetCollection` |
| Supervised classification | ⚠️ partial | `Dataset.sample`/`extract` for training data + client-side sklearn + `Dataset.apply`; training isn't EE and won't scale like server-side `ee.Classifier` |
| `ee.Algorithms` (LandTrendr, CCDC, SNIC, CloudScore+, pansharpen) | ❌ | server-side EE graphs only |
| `ee.Join` (collection joins) | ❌ | server-side only |
| `ee.Model` / Vertex AI inference | ❌ | EE platform service |
| Map tiles (`getMapId` / `getTileUrl`) | ❌ | EE tile server |
| Video export (`Export.video`) | ❌ | EE sink |
| Table / BigQuery / FeatureView exports | ❌ | EE sinks |
| Asset management & ingestion | ❌ | EE platform API |
| Workload tags / profiling / project config | ❌ | EE platform API |
| Modern `computePixels` / `getPixels` API | ❌ (N/A) | EEDAI *is* pyramids-eo's alternative pixel route, so this gap is moot here |

## Summary

- **Roughly half the gaps are implementable client-side via
  `pyramids-eo` / `pyramids`** — after the EEDAI read materialises the pixels.
- Of those, a large chunk **already exist as `pyramids` methods** (terrain,
  zonal stats, vectorize, clustering, COG, cost estimate, SCL mask, RGB
  composites / stretch) and would only need **wiring into the earthlens GEE
  backend**, not new development.
- The rest — ML training/inference at scale, joins, `ee.Algorithms`, tiles,
  video, table/asset exports, ingestion, platform ops — are **server-side Earth
  Engine features** that stay on raw `ee` via the `el.datasource.client` escape
  hatch, because pyramids would have to download the archive to emulate them.

### The honest trade-off

Every ✅ / ⚠️ item works by **downloading pixels first, then computing
locally** — great for a watershed, a city, a study site; not a substitute for
Earth Engine running compute over petabytes and returning a small result. Small
/ medium AOI → pyramids-eo is the natural home. Continental / global scale or
graph algorithms → raw `ee`.

---

*Generated from `earthengine-api==1.7.35`, `pyramids-eo==0.8.1`, and
`pyramids-gis==0.65.0` (all introspected) vs. the earthlens GEE backend source.
Everything in the `ee` list remains accessible today via the
`el.datasource.client` raw-`ee` escape hatch.*
