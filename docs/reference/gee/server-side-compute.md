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
| A temporal composite (`mean` / `median` / …) | `reducer=`, `temporal_resolution=` |
| Scene selection by metadata (cloud cover, orbit, …) | `filters=` |
| A per-image transform — masking, rescaling, dB conversion | `cloud_mask=` |
| Anything else Earth Engine can do | `el.datasource.client` — the raw `ee` module, already authenticated |

Using `filters=` or `cloud_mask=` moves the request onto the Earth Engine engine. That is not a
limitation to work around; it is the only engine that can run a graph.

## The two engines

`engine=` selects **who materialises the pixels** for `export_via="url"`. It does not change the AOI,
the CRS, or the values you asked for.

| | `engine="ee"` | `engine="eedai"` |
|---|---|---|
| Path | `ee.Image.getDownloadURL` | GDAL's `EEDAI` driver via pyramids-eo |
| Server-side compute | **Yes** — the full Earth Engine graph | **No** — it reads stored pixels |
| Size cap | 32768 px per axis (`auto_split=True` tiles around it) | none; oversize windows stream to disk in tiles |
| Round-trip | HTTP + zip | direct read from the asset |
| Extra needed | `[gee]` | `[gee]` **and** `[eedai]` |
| Temporal composite | server-side | client-side, per time bucket |

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
- the reducer is `mosaic` — client-side it means *first scene*, in Earth Engine it means *last wins*, so
  serving it would quietly change the result

Narrow the window, shorten `temporal_resolution`, add a `property_filter`, or use a coarser `scale` to
get back under the budget. These thresholds are internal constants, not public API — treat them as
current behaviour rather than a contract.

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
| `reducer=` | temporal reduction: `mean`, `median`, `min`, `max`, `mode`, `mosaic`, `sum` |
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

## Choosing an engine, in practice

| Situation | Engine |
|---|---|
| Any `cloud_mask` / `filters` / computed graph | `ee` (forced) |
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
- **`cog=True` and `resample=`** apply to the EEDAI path only; the Earth Engine paths ignore them.
- **`auto_split=True`** is only meaningful for `engine="ee"`, where the 32768-px cap exists.
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
