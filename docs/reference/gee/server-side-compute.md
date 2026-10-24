# Server-side computation

Earth Engine is a compute platform, not just an archive: the useful part is usually the graph you build
before any pixel is downloaded — masking, band math, joins, temporal reduction. The `gee` backend does not
hide that. This page shows how to reach it through earthlens, where the boundaries are, and how to fall
through to raw `ee` when the backend's knobs run out.

It also explains the `engine=` choice, because that is the one setting that decides whether server-side
computation is available at all.

## The short version

| You want | Use |
|---|---|
| A temporal composite (`mean` / `median` / `p90` / `trend` / …) | `reducer=`, `temporal_resolution=` |
| The composite back in memory, not only on disk | `materialize=True` → `DownloadResult.data` (a `Dataset` or `DatasetCollection`) |
| Scene selection by metadata (cloud cover, orbit, …) | `filters=` |
| A per-image transform — masking, rescaling, dB conversion | `cloud_mask=` |
| Anything else Earth Engine can do | `el.datasource.client` — the raw `ee` module, already authenticated |

Using `filters=` or `cloud_mask=` moves the request onto an Earth Engine engine. That is not a
limitation to work around; those are the only engines that can run a graph.

## The three engines

`engine=` selects **who materialises the pixels** for `export_via="url"`. It does not change the AOI,
the CRS, or the values you asked for.

| | `engine="ee"` | `engine="pixels"` | `engine="eedai"` |
|---|---|---|---|
| Path | `ee.Image.getDownloadURL` | `ee.data.computePixels` | GDAL's `EEDAI` driver via pyramids-eo |
| Server-side compute | **Yes** — the full Earth Engine graph | **Yes** — the expression is the graph | **No** — it reads stored pixels |
| Size cap | 32768 px per axis (`auto_split=True` tiles around it) | the same 32768 px per axis, same remedy | none; oversize windows stream to disk in tiles |
| Round-trip | HTTP + zip | the GeoTIFF bytes, directly | direct read from the asset |
| Extra needed | `[gee]` | `[gee]` | `[gee]` **and** `[eedai]` |
| Temporal composite | server-side | server-side | client-side, per time bucket |

`"pixels"` is the engine for a request that needs a graph and wants Earth Engine's current extraction API for
it: the GeoTIFF bytes come back directly, with no URL to expire and no zip to unpack, from a grid the request
states outright. It does **not** buy a bigger output — the per-axis cap is the same, and `auto_split=True` is what
gets past it on either path. It is never chosen automatically: `"auto"` keeps preferring the reader and then
`"ee"`, so the preference order is a separate, reviewable decision rather than a side effect of adding an engine.

One sampling difference is worth knowing, and it is deliberate. `"pixels"` sends an explicit grid, so it sizes on
the **metre** convention — square metres on the ground, shortening longitude by `cos(latitude)`, the same rule the
EEDAI reader uses — while `"ee"` lets Earth Engine read `scale` in a geographic CRS as a uniform
degree-equivalent. Away from the equator the two therefore return **different column counts** for one AOI at one
`scale`: at 30° N a degree of longitude is about 13% shorter than a degree of latitude. The AOI, the CRS and the
values agree (a live e2e compares their elevation distributions on the same SRTM window); the grid does not. Pass
`crs_transform=` when the grid itself has to be identical.

`engine="auto"` (the default) picks `eedai` when the request is a *raw read* and the extra is installed,
otherwise `ee`. A request is a raw read when **all** of these hold:

- `export_via="url"` — the asynchronous `drive` / `gcs` / `asset` sinks are Earth Engine-only
- `cloud_mask is None`
- `filters` is empty
- the dataset is `ee_type="image"` or `"image_collection"`
- `crs` is `EPSG:4326` or a **metre-based** projected CRS (a metre `scale` cannot size anything else)

So the rule is simply: **ask for computation and you get the Earth Engine engine.** `engine="eedai"`
raises rather than silently dropping your mask or filters.

### When a collection goes back to Earth Engine anyway

Even an eligible collection is sized before the reader serves it, because the reader downloads every
scene in the bucket and holds them in memory to reduce. It hands the bucket back to Earth Engine when:

- the bucket holds more scenes than the scene cap (currently 500)
- the scene stack exceeds the single-pass pixel budget (currently 2×10⁸ px)
- the reducer is `mosaic` — last-wins in Earth Engine's collection order has no client-side equivalent over
  a scene stack (it would need per-scene masks the reader does not expose), so **mosaic stays on Earth
  Engine**. This is the resolution recorded for that case: `mosaic` is declined client-side and reimplemented
  nowhere, not quietly approximated. Every other temporal reducer — `mean` / `median` / `min` / `max` /
  `mode` / `sum`, and the percentile (`percentile:<q>` / `p<q>`), `trend` and `sens_slope` reducers — is
  computed client-side by pyramids' `reduce_time`, so `median` is a true per-pixel median and a percentile or
  trend no longer forces the bucket back to Earth Engine
- the reducer is a named-algorithm composite — `LandsatComposite` / `Sentinel2Composite` wrap
  `ee.Algorithms.Landsat.simpleComposite` / `ee.Algorithms.Sentinel2.CDI`, which exist only server-side,
  so there is nothing client-side to approximate them with and the bucket runs on Earth Engine

Narrow the window, shorten `temporal_resolution`, add a `property_filter`, or use a coarser `scale` to
get back under the budget. These thresholds are internal constants, not public API — treat them as
current behaviour rather than a contract.

!!! note "Client-side reduction needs pyramids-core >= 0.70.0, and the live EEDAI path has a known gap"
    The client-side composite — the true per-pixel `median`, and the `percentile` / `trend` / `sens_slope`
    reducers — runs through pyramids' `DatasetCollection.reduce_time`, whose `median` / `percentile` /
    `linear_trend` / `sens_slope` operations ship in **pyramids-core >= 0.70.0** (the distribution formerly
    published as `pyramids-gis`; the import name stays `pyramids`). Below that version these reductions are
    unavailable. The no-nodata caveat carries into them: the EEDAI driver declares no fill, so a statistical
    reducer folds a scene's fill pixels into the result where Earth Engine would have masked them — the two
    agree only where the scenes carry no fill over the AOI.

    **Known limitation — live EEDAI reduction.** `pyramids_eo.collection_from_earthengine` currently returns
    an *in-memory* (`datasets=`-backed) `DatasetCollection`, which `reduce_time` cannot consume — it reads a
    file- or zarr-backed collection — so a live `engine="eedai"` collection composite (even `median`) raises
    a `RuntimeError` today. The earthlens wiring is correct, and the reduction is verified against `numpy` on
    a file-backed stack; it will work end to end once that upstream reader gap is closed. Until then, pass
    `engine="ee"` for a collection composite.

### The two engines are not byte-identical

Worth knowing before you compare outputs:

- **Grid.** Earth Engine reads `scale` in a geographic CRS as a uniform degree-equivalent; the EEDAI grid
  is sized for square metres on the ground. Away from the equator the column counts differ.
- **Resampling.** The reader warps from the asset's native resolution locally (`nearest` by default) where
  Earth Engine aggregates server-side. For continuous fields read *coarser* than native — elevation,
  temperature, reflectance — `resample="average"` is much closer to Earth Engine; keep `nearest` for
  categorical data such as land cover.
- **Nodata in a composite.** The EEDAI driver declares no fill value, so the reader's `mean` / `median` /
  `min` / `max` / `sum` run unmasked and fold a scene's fill pixels into the result where Earth Engine
  would have masked them. They agree wherever the scenes carry no fill over the AOI. When you need Earth
  Engine's masking exactly, pass `engine="ee"`.

The AOI, the CRS and the values agree. The sampling does not.

## Level 1 — declarative knobs, no `ee` code

Several Earth Engine operations are plain constructor arguments. Nothing here needs you to import `ee`.

| Argument | Server-side operation |
|---|---|
| `reducer=` | temporal reduction: `mean`, `median`, `min`, `max`, `mode`, `mosaic`, `sum`, a percentile (`percentile:<q>` / `p<q>`), `trend`, `sens_slope` |
| `temporal_resolution=` | `raw` / `daily` / `monthly` / `yearly` bucketing; each bucket is reduced independently |
| `variables=` | `select(bands)` |
| `scale=`, `crs=`, `resample=` | resampling and reprojection |
| `region=` | a `GeoDataFrame` cutline — clipped precisely, superseding the bbox |
| `property_filter=` | scene-metadata filter string (**EEDAI path only**, see below) |

A monthly median composite over a polygon, entirely declarative:

```python
from earthlens.core import EarthLens

el = EarthLens(
    data_source="gee",
    dataset="COPERNICUS/S2_SR_HARMONIZED",
    variables=["B4", "B3", "B2"],
    start="2024-05-01",
    end="2024-08-31",
    lat_lim=[29.9, 30.1],
    lon_lim=[31.2, 31.4],
    temporal_resolution="monthly",
    reducer="median",
    scale=10,
    path="out",
).authenticate(service_account=SERVICE_ACCOUNT, service_key=SERVICE_KEY)

paths = el.download()
```

The catalog also encodes reduction advice you inherit for free. `COPERNICUS/S1_GRD` declares
`default_reducer: median` precisely because backscatter is in dB and averaging dB is not averaging power —
so omitting `reducer=` on Sentinel-1 gives you the defensible choice, not an arithmetic mistake.

### Named-algorithm composites

`reducer=` also accepts a `TemporalReducer` *instance* — `Percentile(q=90)` works — and that is how Earth
Engine's named-algorithm composites plug in, since they carry config no string can spell:

```python
from earthlens.gee import LandsatComposite, Sentinel2Composite

reducer=LandsatComposite(percentile=50, cloud_score_range=10, max_depth=40, as_float=True)
reducer=Sentinel2Composite(threshold=-0.5, reducer="median")
```

- `LandsatComposite` runs `ee.Algorithms.Landsat.simpleComposite` over each time bucket: a cloud-screened,
  TOA-calibrated mosaic in one server call. **It needs the raw Level-1 collection** — the curated
  `LANDSAT/LC08/C02/T1` catalog row is the example — not the `T1_L2` surface-reflectance product, because
  the algorithm performs its own TOA calibration.
- `Sentinel2Composite` maps the `Sentinel2Cdi` mask (`ee.Algorithms.Sentinel2.CDI`, the parallax-based
  cloud discriminator) over every scene, then reduces the masked stack with its inner `reducer` (median by
  default). CDI is defined on the **TOA** product (`COPERNICUS/S2_HARMONIZED`), not L2A. The standalone
  `Sentinel2Cdi(threshold=...)` mask is also an ordinary `cloud_mask=` drop-in.

Both are validated at construction like every reducer (`GEE(reducer=...)` resolves instances through the
same `temporal_reducer_for` seam the strings use), are deliberately *not* in the `REDUCERS` registry, and
exist **only server-side**: the EEDAI reader declines them (see the collection routing above), so the
request runs on an Earth Engine engine.

!!! note "`property_filter` is not `filters`"
    `property_filter="CLOUDY_PIXEL_PERCENTAGE < 20"` is an **OGR attribute-filter string** consumed by the
    EEDAI reader, so it narrows scene selection *without* forcing the Earth Engine engine. `filters=` are
    Earth Engine closures with no string form, which is why the two cannot be the same knob.
    `property_filter` is ignored (with a warning) for a single image or an Earth Engine-served request.

## Level 2 — `filters=`, collection-level compute

`filters` takes an iterable of `Callable[[ee.ImageCollection], ee.ImageCollection]`, composed left to
right. Anything you can do to a collection belongs here: metadata filters, `.sort`, `.limit`, joins,
your own `.map`.

`earthlens.gee.filters` ships the common ones — `by_year`, `by_bounds`, `by_property_in`,
`by_cloud_cover_lte`, `by_year_and_bounds`:

```python
import ee

from earthlens.gee import filters

el = EarthLens(
    data_source="gee",
    dataset="COPERNICUS/S2_SR_HARMONIZED",
    variables=["B8", "B4"],
    start="2024-05-01",
    end="2024-09-01",
    lat_lim=[29.9, 30.1],
    lon_lim=[31.2, 31.4],
    reducer="median",
    filters=[
        lambda c: filters.by_cloud_cover_lte(c, 20, property_name="CLOUDY_PIXEL_PERCENTAGE"),
        lambda c: c.filter(ee.Filter.eq("SENSING_ORBIT_NUMBER", 79)),
    ],
    path="out",
)
```

`by_cloud_cover_lte` defaults to Landsat's `CLOUD_COVER`, so Sentinel-2 needs
`property_name="CLOUDY_PIXEL_PERCENTAGE"` passed explicitly.

Each of these also exists as a frozen callable class — `ByYear`, `ByBounds`, `ByPropertyIn`,
`ByCloudCoverLte`, `ByYearAndBounds`, under the `earthlens.gee.filters.CollectionFilter` base — whose
instances carry validated config and are drop-ins for a `filters=` entry, so the first lambda above can
lose its wrapper:

```python
from earthlens.gee.filters import ByCloudCoverLte

filters=[ByCloudCoverLte(max_pct=20, property_name="CLOUDY_PIXEL_PERCENTAGE")]
```

A typo'd keyword or an out-of-range `max_pct` is refused at construction (`frozen=True`,
`extra="forbid"`), where a `functools.partial` would bind the misspelling silently. One naming note: the
top-level `earthlens.gee.CollectionFilter` stays the `Callable` *type alias* describing the hook; the
class of the same name in `earthlens.gee.filters` is the base that implements it. Class or function, any
filter moves the request onto an Earth Engine engine, as above.

### The expanded filter library

Six further class-only selectors cover what SAR, multi-sensor and seasonal work needs — each a frozen,
construction-validated `CollectionFilter` drop-in for a `filters=` entry (and, like every filter, an
Earth Engine-engine request):

- `ByOrbit(pass_direction=..., relative_orbit=...)` — pins a Sentinel-1 pass direction
  (`orbitProperties_pass`) and/or relative orbit (`relativeOrbitNumber_start`). Ascending and
  descending passes have different look geometries, so mixing them in a composite is almost always
  wrong; at least one constraint is required.
- `BySensor(values=("LANDSAT_8", "LANDSAT_9"))` — platform selection via `ee.Filter.inList`; the
  default property matches Landsat C2 (`SPACECRAFT_ID`), Sentinel-2 passes
  `property_name="SPACECRAFT_NAME"`.
- `ByQuality(property_name=..., op="lte", value=...)` — the generic metadata-threshold escape hatch,
  dispatching to `ee.Filter.{lte,gte,lt,gt,eq}`, so a new threshold needs no bespoke class.
- `BySeason(season="JJA")` — one meteorological season (`DJF` / `MAM` / `JJA` / `SON`) *across every
  year* in the date window, expanded to an `ee.Filter.Or` over its months — the same idiom
  `Climatology` uses, so `DJF` wraps the year boundary correctly.
- `ByMonth(start_month=6, end_month=8)` / `ByDayOfYear(start_day=152, end_day=243)` — calendar windows
  across every year via `ee.Filter.calendarRange`; a start after the end wraps (Earth Engine
  semantics), and omitting the end selects the single period.

```python
from earthlens.gee.filters import ByOrbit, BySeason

filters=[ByOrbit(pass_direction="ASCENDING", relative_orbit=87), BySeason(season="JJA")]
```

## Level 3 — `cloud_mask=`, per-image compute

`cloud_mask` is typed `Callable[[ee.Image], ee.Image]` and is `.map`-applied to every image in the
collection. **Despite the name it is a general per-image hook**, not only a mask — any `ee.Image`
expression is fair game.

`earthlens.gee.cloud_masks` provides two ready-made masks:

- `sentinel2_scl(image)` — Sentinel-2 L2A, dropping SCL cloud shadow (3), cloud medium/high probability
  (8 / 9) and thin cirrus (10). It deliberately **keeps** saturated/defective (1) and snow/ice (11).
- `landsat_sr(image, sensor=...)` — Landsat C2-L2, keeping the `QA_PIXEL` Clear bit (bit 6).

```python
from earthlens.gee.cloud_masks import sentinel2_scl

el = EarthLens(
    data_source="gee",
    dataset="COPERNICUS/S2_SR_HARMONIZED",
    variables=["B4", "B3", "B2"],
    start="2024-05-01",
    end="2024-06-15",
    lat_lim=[29.9, 30.1],
    lon_lim=[31.2, 31.4],
    reducer="median",
    cloud_mask=sentinel2_scl,
    path="out",
)
```

Both masks also ship as frozen callable classes — `LandsatSrMask(sensor=...)` and
`Sentinel2SclMask(masked_classes=...)`, under the `earthlens.gee.cloud_masks.ImageMask` base — validated
at construction and drop-ins for `cloud_mask=`:

```python
from earthlens.gee.cloud_masks import Sentinel2SclMask

cloud_mask=Sentinel2SclMask(masked_classes=(3, 8, 9, 10, 11))   # also drop snow/ice
```

The masks derive their output with `updateMask`, which keeps image properties — `system:time_start` in
particular — so the per-bucket date filter the composite engine applies *after* the mask still sees every
scene. Like the function forms, an instance on `cloud_mask=` forces the request onto an Earth Engine
engine (the EEDAI reader cannot run a graph).

### The expanded mask library

Three further `ImageMask` classes broaden coverage across the catalog's optical datasets. Each reads a
quality band its target dataset declares in the curated catalog, derives its output with `updateMask`
(properties survive), and — like every `cloud_mask=` — moves the request onto an Earth Engine engine:

- `Sentinel2Qa60Mask()` — the standard pre-Scene-Classification recipe: masks the `QA60` opaque-cloud
  bit (10) and cirrus bit (11). Use it for TOA scenes (`COPERNICUS/S2_HARMONIZED`), which carry `QA60`
  but no `SCL` band.
- `LandsatQaRefinedMask(sensor=..., max_cloud_confidence=2, max_shadow_confidence=2)` — the per-sensor
  refinement the `sensor` argument was reserved for: thresholds the `QA_PIXEL` Cloud Confidence
  (bits 8-9) and Cloud Shadow Confidence (bits 10-11) fields instead of trusting the single derived
  Clear bit. Ceilings are `0` (not set) to `3` (high), validated at construction.
- `ModisState1kmMask()` — the daily MODIS surface-reflectance products (`MODIS/061/MOD09GA` /
  `MYD09GA`): keeps only pixels whose 2-bit cloud-state field (bits 0-1 of `state_1km`) reads
  "clear" (0).

On a dataset that does not carry the mask's quality band, Earth Engine fails at graph-compute time —
the same contract as the first gotcha below; the curated rows named above all declare theirs.

### `CloudScorePlus` — the state-of-the-art Sentinel-2 mask

`CloudScorePlus` masks a Sentinel-2 scene with Google's Cloud Score+ dataset
(`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`). The scores live in a *separate* collection aligned to the
harmonized S2 archive, so the mask links each scene to its score image with `linkCollection` — a
per-image match on `system:index` — then keeps pixels scoring at or above `threshold`. Because the link
is per image, it works unchanged under `collection.map(...)` and is an ordinary `cloud_mask=` drop-in:

```python
from earthlens.gee.cloud_masks import CloudScorePlus

cloud_mask=CloudScorePlus()                          # band="cs", threshold=0.6 — Google's recipe
cloud_mask=CloudScorePlus(band="cs_cdf", threshold=0.5)
```

The two score bands differ: `cs` scores each pixel on its own; `cs_cdf` integrates a cumulative
distribution and tends to be more conservative over bright surfaces. The threshold is a `[0, 1]` quality
score (not a percentage) and is validated at construction. Cloud Score+ folds shadow signal into both
bands, so no separate shadow projection is applied. Reading the second collection is server-side compute,
so this mask — like every `cloud_mask=` — runs on an Earth Engine engine, never the EEDAI reader.

### Ordering, and why it matters

The chain is:

```
filterDate → filterBounds → filters (left to right) → cloud_mask (.map) → select(bands)
```

`cloud_mask` runs **before** `select` on purpose: an optical mask reads a quality band (`QA_PIXEL`, `SCL`)
that your `variables` usually omit, so selecting first would strip the band the mask needs.

### The one real constraint: band names must be in the catalog

Every requested band is validated against the catalog before the graph is built, so **a band your hook
creates cannot be requested**:

```python
>>> from earthlens.gee import Catalog
>>> Catalog().get_dataset("COPERNICUS/S2_SR_HARMONIZED").get_band("NDVI")
Traceback (most recent call last):
    ...
ValueError: 'NDVI' is not a band of 'COPERNICUS/S2_SR_HARMONIZED'. Known bands: [...]
```

So `cloud_mask` is the right seam for **transforming** bands that already exist — masking, rescaling to
physical units, dB conversion, a per-image correction — and the wrong seam for adding a new named output.

If you want a computed index written to disk, pick one of:

1. **Overwrite a declared band.** Works today, at the cost of an honest name:

    ```python
    def ndvi_into_b8(image):
        nd = image.normalizedDifference(["B8", "B4"]).rename("B8")
        return image.addBands(nd, overwrite=True)
    ```

    Request `variables=["B8"]` and the file carries NDVI under the name `B8`. The catalog's `min` / `max` /
    `scale` metadata for `B8` no longer describes the contents, so this is a deliberate trade, not a
    recommendation.

2. **Drive `ee` yourself** via the escape hatch below — the clean route for genuinely new outputs.

3. **Compute it downstream** from the downloaded bands with pyramids/NumPy, which keeps the archive
   request and the derived product separate.

### Values are raw DN, not physical units

The catalog records each band's `scale` (and `offset`) as metadata — `Band.scale` is documented as
"multiply the raw DN by this to get physical units" — but **nothing applies it**. Sentinel-2 surface
reflectance arrives as 0–10000 integers, not 0–1 reflectance. Scale it yourself, in a `cloud_mask` hook
if you want it done server-side before the composite:

```python
def to_reflectance(image):
    return image.multiply(0.0001).copyProperties(image, image.propertyNames())
```

Note the `copyProperties`: `multiply` drops image metadata, which any metadata-dependent `filters` running
after it would then miss.

## Level 4 — `.client`, the full escape hatch

When the knobs run out, take the authenticated `ee` module and do whatever Earth Engine does. You do not
re-do authentication; `client` is a lazy property that returns the initialized module.

```python
el = EarthLens(
    data_source="gee",
    dataset="COPERNICUS/S1_GRD",
    variables=["VV"],
    start="2024-01-01",
    end="2024-03-01",
    lat_lim=[29.9, 30.1],
    lon_lim=[31.2, 31.4],
    path="out",
).authenticate(service_account=SERVICE_ACCOUNT, service_key=SERVICE_KEY)

gee = el.datasource          # the GEE backend instance
ee = gee.client              # the initialized `ee` module
print(gee.project)           # the resolved Cloud project

s1 = (
    ee.ImageCollection("COPERNICUS/S1_GRD")
    .filterBounds(ee.Geometry.Rectangle([31.2, 29.9, 31.4, 30.1]))
    .filterDate("2024-01-01", "2024-03-01")
    .filter(ee.Filter.eq("instrumentMode", "IW"))
)
speckle_filtered = s1.map(lambda img: img.focal_median(30, "circle", "meters"))
composite = speckle_filtered.select("VV").median()
```

From here you have full parity with the Code Editor: `ee.Algorithms`, `reduceRegion(s)`, `ee.Join`,
`ee.Classifier`, `ee.Reducer` combinations, `ee.Image.expression`, arbitrary `.map`.

### Getting results back into Python

earthlens meets you on the return trip, so you do not hand-roll `getInfo()` paging:

| Helper | Does |
|---|---|
| `sample_points(image, gdf, scale_m=..., reducer=...)` | `reduceRegions` over point geometries, server-side |
| `sample_points_to_gdf(...)` | the same, returned as a `GeoDataFrame` |
| `feature_collection_to_gdf(fc)` / `feature_collection_to_dataframe(fc)` | download an `ee.FeatureCollection` |
| `feature_collections_to_dataframe([...])` | the parallel variant over many collections |
| `create_geometry` / `create_feature` | Shapely / `GeoDataFrame` → `ee.Geometry` / `ee.FeatureCollection` |

```python
from earthlens.gee import sample_points_to_gdf

sampled = sample_points_to_gdf(composite, stations_gdf, scale_m=10, reducer="mean")
```

### Exporting a graph you built yourself

For a computed image too large for `getDownloadURL`, use the asynchronous sinks. Set
`wait_for_export=False` and `download()` returns `TaskInfo` objects at submission time instead of
blocking; `list_recent_tasks`, `get_task_status`, `wait_for_task_id`, `cancel_task` and
`resolve_destination` track them. The batch sinks are Earth Engine-only, so `engine=` is ignored there.

### Temporal helpers over a collection

Four frozen, validated config objects build a server-side graph over an `ee.ImageCollection` you hold — the
`.client` collection above, or one you built yourself. They answer the questions a single reducer cannot:
*when did it change*, *how does this period compare to normal*, *which scene is nearest a date*, and *fill the
holes before reducing*. Each is imported from `earthlens.gee`.

`TemporalSegmentation` (`LandTrendr` / `Ccdc`) finds breakpoints in a per-pixel trajectory — disturbance and
recovery — and returns a `SegmentationResult` wrapping the algorithm's array-valued `ee.Image`, never a
plottable raster. Mask clouds first; the algorithms assume a clean series:

```python
from earthlens.gee import LandTrendr

nbr = masked_collection.map(lambda img: img.normalizedDifference(["B5", "B7"]).rename("NBR"))
result = LandTrendr(maxSegments=6).segment(nbr)   # invert=True by default for a vegetation index
vertices = result.array                            # the ee.Array image, for your own getInfo/export
```

`Climatology` reduces a collection *per calendar period across a baseline* (`dayofyear` / `month` / `season`),
and `anomaly()` is the observed-minus-baseline companion:

```python
from earthlens.gee import Climatology

clim = Climatology(period="month", baseline=(1991, 2020), reducer="mean")
monthly = clim.climatology(collection)                       # one image per month
june_2024 = clim.anomaly(collection, collection_2024, 6)     # this June vs the 1991-2020 June mean
```

`ClosestImage` selects the scene nearest a target date, within an optional tolerance (an out-of-range target
with a tolerance set yields an empty selection rather than a distant scene):

```python
import datetime as dt

from earthlens.gee import ClosestImage

nearest = ClosestImage(target=dt.date(2024, 6, 1), tolerance=dt.timedelta(days=15)).select(collection)
```

`GapFill` fills temporal holes *before* a reduction or sampling, by `linear` / `nearest` / `harmonic`
interpolation over a within-`max_gap` neighbour join — `max_gap` is a refusal, not a hint, so a pixel with no
valid neighbour in the window stays masked:

```python
import datetime as dt

from earthlens.gee import GapFill

filled = GapFill(method="linear", max_gap=dt.timedelta(days=30)).fill(collection)
```

## Choosing an engine, in practice

| Situation | Engine |
|---|---|
| Any `cloud_mask` / `filters` / computed graph | `ee` (forced) |
| A computed graph *and* an AOI over 32768 px | `ee` or `pixels`, either with `auto_split=True`, or `drive` |
| An output grid that must align with an earlier download | `ee` or `pixels` with `crs_transform=` |
| Raw pixels from one asset, large AOI | `eedai` — no 32768-px cap, no zip round-trip |
| A composite whose masking must match Earth Engine exactly | `ee` |
| Long time series over a small AOI | `eedai`, watching the scene cap |
| `export_via` other than `"url"` | `ee` (the sinks are Earth Engine-only) |
| Not sure | leave `engine="auto"` |

## Gotchas

- **`cloud_mask` and `filters` on a static `ee_type="image"` dataset** are applied verbatim. A filter can
  reject the single wrapped image and empty the collection; a mask reading an absent band fails when the
  graph is computed. The backend logs a warning but cannot validate it upfront — it surfaces as an opaque
  Earth Engine error at download time.
- **Mixed-resolution band requests** (10 m `B4` + 20 m `B11` + 60 m `B1` in one call) are a sharp edge on
  the EEDAI path. Request one resolution group at a time, or use `engine="ee"`.
- **`cog=True`** applies to the paths that write the raster themselves — the EEDAI reader and
  `engine="pixels"`. `getDownloadURL` and the batch sinks ignore it, and so does `resample=`, which is the
  EEDAI reader's warp kernel alone.
- **`nodata=`** is honoured by `engine="pixels"` on the `"url"` sink (the expression is unmasked with it and
  the written raster declares it) and by `export_via="drive"` / `"gcs"` (Earth Engine's own
  `GeoTiffImageExportOptions.noData`). It is **not** a field on the pixel API's request — a body carrying one
  is refused with `Unknown name "nodata" at 'grid'` — and an asset export has no file format at all, so
  that sink and `getDownloadURL` ignore it and warn. Without it, a masked composite's pixels come back as 0
  with nothing declared.
- **`high_volume=`** is `engine="pixels"` only; any other engine warns and initialises against the regular
  endpoint.
- **`auto_split=True`** applies to `engine="ee"` and `engine="pixels"`, which share Earth Engine's
  32768-px per-axis cap. Measured, because the guide implies otherwise: a `computePixels` request of
  39,479 × 12 px comes back as `Pixel grid dimensions (39479x12) must be less than or equal to
  32768`. The reader has no such cap, so `auto_split` means nothing to it.
- **Credentials are always needed**, even for `engine="eedai"`: the request is built through `ee` before
  its pixels are fetched.
- **No Sentinel-1 preprocessing** ships in the backend — no speckle filter, no terrain correction. Note
  that Earth Engine's own `COPERNICUS/S1_GRD` is already thermal-noise-removed, calibrated and
  terrain-corrected to dB upstream; speckle filtering is the genuine gap, and belongs in a `cloud_mask`
  hook or the escape hatch.

## See also

- [Usage](usage.md) — the request shape, extents, and export sinks
- [Service account setup](service-account-setup.md) and [Registering a project](registering-a-project.md)
- [Reference](gee.md) — the generated API surface
