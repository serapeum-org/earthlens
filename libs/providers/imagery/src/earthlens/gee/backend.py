"""Google Earth Engine backend — :class:`GEE`, an :class:`AbstractDataSource`.

Downloads imagery from Google Earth Engine. A request is `{asset_id:
[band, ...], ...}` (the addressable units of an EE dataset are *bands*,
and one image carries many at once), plus a date range, a bbox (or a
`GeoDataFrame` region), a temporal-compositing resolution
(`"raw"`/`"daily"`/`"monthly"`/`"yearly"`), and an output pixel `scale`
in metres. The asset ids and band metadata are resolved through
:class:`earthlens.gee.Catalog` (loaded from the per-category YAMLs
under `src/earthlens/gee/catalog/`).

Per `(asset, band-set, time-bucket)` the pipeline is:

* :meth:`_build_collection` — `ee.ImageCollection(asset_id)` (or the
  single `ee.Image` wrapped in one), `.filterDate(...)`,
  `.filterBounds(region)`, then any constructor `filters` and the
  per-image `cloud_mask` (`.map`-applied), and finally `.select(bands)`.
  Pure: no I/O.
* :meth:`_composite` — split the request window into buckets at the
  requested cadence and collapse each with the dataset's
  `default_reducer` (or the constructor `reducer` override) — `mean`
  for continuous fields / rates, `median` for cloud-screened optical
  scenes, `mosaic` for tiled / annual static maps. Yields one
  `ee.Image` per bucket.
* the EEDAI fast-path — a raw read of a materialised asset, or a
  client-side composite of an `ImageCollection`, served by the pyramids-eo
  reader in EPSG:4326 or a metre-based projected CRS; see
  :meth:`_eedai_eligible` for what qualifies and :meth:`_eedai_verdict` for
  what it costs.
* :meth:`_api` — export the bucket image via the configured
  `export_via`: `"url"` (the default) computes the request's pixel
  dimensions and refuses if either axis exceeds Earth Engine's 32768-px
  synchronous limit (a clear, actionable `ValueError`), else
  `image.getDownloadURL({..., "format": "GEO_TIFF"})` → an `HttpClient`
  GET → a GeoTIFF under the output directory; multi-band responses (which
  Earth Engine returns as a zip of per-band tifs) are unpacked through
  `pyramids.dataset.Dataset.from_archive` into a single multi-band tif.
  `"drive"` / `"gcs"` queue an asynchronous
  `ee.batch.Export.image.to{Drive,CloudStorage}` task (`maxPixels` only,
  no 32768-px cap), poll it to completion, and return a `"drive://…"` /
  `"gs://…"` destination string (the file is left in the Drive folder /
  GCS bucket for the caller to pull).

Authentication is a one-time `ee.Initialize` against a *registered*
Cloud project, performed by :meth:`_initialize` via
:class:`earthlens.gee.auth.EarthEngineAuth` (service-account key) or, if
no key is given, an interactive `ee.Authenticate()` against an explicit
`project`. Credential / registration failures surface as
:class:`AuthenticationError`.
"""

from __future__ import annotations

import datetime as dt
import gc
import math
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import ee
import pandas as pd
from loguru import logger
from pyramids.dataset import Dataset as PyramidsDataset
from pyramids.dataset.merge import merge_rasters
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    LazyClientMixin,
    OutputKind,
    TemporalExtent,
    close_quietly,
    date_windows,
    to_datetime,
)
from earthlens.gee._eedai import (
    credentials_for,
    eedai_available,
    import_earthengine_reader,
)
from earthlens.gee._helpers import (
    EE_MAX_DIMENSION,
    reduce_collection,
    slug_asset_id,
    split_aoi_for_url,
    wait_for_task,
)
from earthlens.gee.auth import AuthenticationError, EarthEngineAuth
from earthlens.gee.catalog import Catalog, Dataset
from earthlens.gee.features import create_feature
from earthlens.gee.jobs import TaskInfo, _op_to_taskinfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas import GeoDataFrame

    from earthlens.base.http import HttpClient

__all__ = [
    "GEE",
    "AuthenticationError",
    "CloudMask",
    "CollectionFilter",
    "EedaiPlan",
]

# `temporal_resolution` → pandas frequency alias for the per-bucket
# date range. `"raw"` is special-cased (one bucket spanning the whole
# request window).
_RESOLUTION_FREQ: dict[str, str] = {"daily": "D", "monthly": "MS", "yearly": "YS"}

_DEFAULT_HTTP_TIMEOUT_S: float = 300.0

#: The only output CRS the EEDAI path serves. The reader takes its `bbox` in
#: the target CRS while this backend's AOI is lat/lon, so a projected `crs`
#: would silently read the wrong ground area; those requests stay on Earth
#: Engine (see `GEE._eedai_eligible`).
_EEDAI_NATIVE_CRS: str = "EPSG:4326"

#: Output pixels per side of one streamed tile when an EEDAI read is too big to
#: materialise in one piece. It is only a ceiling: :meth:`GEE._eedai_single_image_plan`
#: shrinks it until one tile's *native* read fits both budgets below, since the
#: reader materialises that native window in memory per tile.
_EEDAI_TILE_PIXELS: int = 2048

#: Most tiles one streamed read may be split into. The mosaic step opens every
#: tile at once, and each tile is its own fetch-warp-write round trip, so a
#: request needing more than this is refused rather than started. Set high
#: enough that tiling can serve a near-native read past Earth Engine's
#: 32768-px cap, which is the case tiling exists for.
_EEDAI_MAX_TILES: int = 1024

#: Native pixels the reader adds around each tile's window: it widens the
#: window by one pixel at the start and two at the end (`_native_pixel_window`)
#: and allocates exactly that window. Block alignment governs how the window is
#: *walked*, not how much is held, so the footprint is the window plus three
#: pixels per axis — not a block per side.
_EEDAI_WINDOW_PAD: int = 3

#: How much coarser than the asset's own resolution a read may be and still be
#: worth tiling. Above this Earth Engine wins outright: it aggregates
#: server-side and returns a small raster, where the reader would fetch
#: `ratio**2` native pixels per output pixel just to discard most of them.
_EEDAI_MAX_TILING_RATIO: float = 4.0

#: Total native pixels (across every tile and band) one streamed read may
#: fetch. The tile budget bounds memory; this bounds the *work* — bytes over
#: the wire, quota and wall clock — which tile size alone does not.
_EEDAI_MAX_NATIVE_PIXELS: int = 4_000_000_000

#: Total pixels (per band) the EEDAI reader may materialise for one read. The
#: driver has no overviews worth trusting, so it fetches the AOI at the asset's
#: native resolution into memory before downsampling.
_EEDAI_MAX_PIXELS: int = 200_000_000

#: Reducers the reader must not serve, because its client-side result would not
#: match Earth Engine's server-side one. `mosaic` is the clear case: Earth
#: Engine's is last-wins (later scenes paint over earlier), while the reader
#: without a nodata value returns the *first* scene of the stack wholesale. It
#: is also the most common `default_reducer` in the catalog, so this decline is
#: what keeps a composite from silently becoming "the earliest scene".
_EEDAI_UNSUPPORTED_REDUCERS: frozenset[str] = frozenset({"mosaic"})

#: Most scenes a collection composite may fetch through the reader in one bucket.
#: Each scene is a separate download the reader holds in memory to reduce, so a
#: long time series is routed to Earth Engine's server-side reduce instead.
_EEDAI_MAX_SCENES: int = 500

#: The only resampler a tiled read may use. Upstream refuses anything else,
#: because an interpolating kernel would disagree with the un-tiled result at
#: the tile seams.
_EEDAI_TILING_RESAMPLE: str = "nearest"

#: Metres per degree of latitude on a sphere of Earth's mean radius. Used to
#: turn the EEDAI path's metre `scale` into a pixel grid over a lat/lon AOI;
#: longitude is scaled by `cos(latitude)` at the AOI's mid-latitude.
_METRES_PER_DEGREE: float = 111_320.0

#: Accepted `engine` values: which layer materialises the pixels.
_ENGINES: frozenset[str] = frozenset({"auto", "ee", "eedai"})
_ZIP_MAGIC: bytes = b"PK\x03\x04"

#: A per-image cloud/quality mask — `ee.Image -> ee.Image` — `.map`-applied
#: to the collection before the reducer (see `earthlens.gee.cloud_masks`).
CloudMask = Callable[[ee.Image], ee.Image]

#: An `ee.ImageCollection` filter — collection in, filtered collection out —
#: applied after `filterDate` / `filterBounds` (see `earthlens.gee.filters`).
CollectionFilter = Callable[[ee.ImageCollection], ee.ImageCollection]

# Cache of EE-discovered temporal extents per asset id, shared across
# all `GEE` instances in this process. `_discover_ee_extent` issues an
# ~2-5 s `reduceColumns(minMax)` round trip per asset — far too
# expensive to repeat for a second `GEE(...)` construction against the
# same asset. The cache mirrors `_CATALOG_CACHE` / `_PROVIDERS_CACHE`
# in `catalog.py`; clear it via :func:`clear_extent_cache` (mostly for
# tests).
_EXTENT_CACHE: dict[str, tuple[dt.datetime | None, dt.datetime | None]] = {}


def clear_extent_cache() -> None:
    """Forget every cached EE-discovered temporal extent.

    Mirrors :func:`earthlens.gee.catalog.clear_catalog_cache` for the
    `discover_extent=True` cache. Primarily useful in tests that need
    a fresh cache between runs (or when the EE-side data was updated
    and the in-process cache has gone stale).
    """
    _EXTENT_CACHE.clear()


def _validate_pure_config(
    start: str,
    end: str,
    temporal_resolution: str,
    fmt: str,
) -> None:
    """Validate the cheap (no-I/O) config inputs before the catalog parse.

    `Catalog()` triggers a ~3.3 s cold-cache YAML parse + pydantic
    validation. Pure-config errors that the constructor can detect
    without it (bad date format, unknown `temporal_resolution`,
    `start > end`) should fail *before* paying that cost — otherwise
    the user waits 3 seconds to learn they typed `"2024-13-01"`. This
    helper duplicates the cheap checks from :meth:`_check_input_dates`
    so they fire pre-`super().__init__()`; the parent still runs the
    same checks again, but by then they always pass.
    """
    if temporal_resolution != "raw" and temporal_resolution not in _RESOLUTION_FREQ:
        raise ValueError(
            "temporal_resolution must be 'raw', 'daily', 'monthly', or "
            f"'yearly', got {temporal_resolution!r}"
        )
    try:
        start_dt = to_datetime(start, fmt)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"start={start!r} is not parseable with fmt={fmt!r}: {exc}"
        ) from exc
    try:
        end_dt = to_datetime(end, fmt)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"end={end!r} is not parseable with fmt={fmt!r}: {exc}"
        ) from exc
    if start_dt > end_dt:
        raise ValueError(
            f"start={start!r} is after end={end!r}; the date range must be "
            "non-empty (`start <= end`)."
        )


def _rename_when_unlocked(source: Path, target: Path) -> None:
    """Rename `source` onto `target`, retrying once past a lingering GDAL lock.

    A tiled read leaves GDAL handles on the mosaic it just wrote, and on
    Windows those can keep the file locked for a moment after the reader is
    closed — long enough for the rename to fail with `PermissionError` after
    every tile has already been fetched. Collecting first drops the last
    references, which is the same remedy pyramids-eo applies internally, and
    the rename is then retried once.

    Args:
        source: The staged raster to move.
        target: Its final path.

    Raises:
        OSError: If the rename still fails after the retry — losing the
            output silently would be far worse than surfacing it.
    """
    try:
        os.replace(source, target)
    except PermissionError:
        # Collecting only helps once the caller has dropped its own reference
        # to the dataset — see `_export_via_eedai`, which clears it first.
        gc.collect()
        os.replace(source, target)


def _discard_quietly(path: Path) -> None:
    """Remove a staging file, tolerating a lock that outlives its reader.

    Same lingering-handle hazard as :func:`_rename_when_unlocked`, but the
    stakes are reversed: this runs after the real output is already in place,
    so a failure here costs a stray temp file while raising would mask a
    successful download. Sidecars GDAL may have written next to the raster
    (`.aux.xml` and friends) are removed with it.

    Args:
        path: The staging raster to remove; a missing file is not an error.
    """
    # Sidecars either append to the name (`x.tif.aux.xml`) or replace the
    # extension (`x.tfw`, `x.prj`), so both shapes are swept.
    for stray in (path, *path.parent.glob(f"{path.stem}.*")):
        try:
            stray.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug(f"Could not remove the staging file {stray}: {exc}")


def _validate_property_filter(property_filter: object) -> None:
    """Reject a scene filter that would not survive being wrapped by the reader.

    Upstream splices the value in as `f"{time_filter} AND ({property_filter})"`
    without escaping, so a fragment that closes the wrapper early escapes it:
    `1=1) OR (1=1` becomes `time AND (1=1) OR (1=1)`, and the time and space
    clauses stop constraining anything. Counting parentheses does not catch
    that - the totals balance - so nesting depth is tracked instead, and depth
    must never go negative.

    Quoted literals are skipped while scanning, so a `;` or `--` inside a string
    value is allowed while a bare one is not.

    This is a structural check, not a parser, and it is not a security boundary:
    build the filter in code, never from untrusted input.

    Args:
        property_filter: The candidate filter.

    Raises:
        ValueError: It is not a string, is blank, closes a parenthesis it never
            opened, leaves one open, has an unterminated quote, or carries a
            bare statement separator or SQL comment.
    """
    if not isinstance(property_filter, str):
        raise ValueError(
            "property_filter must be an OGR attribute-filter string "
            f"(e.g. 'CLOUDY_PIXEL_PERCENTAGE < 20'), got {property_filter!r}"
        )
    text = property_filter.strip()
    if not text:
        raise ValueError("property_filter must not be blank; pass None instead.")

    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            # A doubled quote is an escaped one inside the literal.
            if char == quote:
                if text[index + 1 : index + 2] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(
                    "property_filter closes a parenthesis it never opened, which "
                    "would escape the filter it is combined with: "
                    f"{property_filter!r}"
                )
        elif char == ";" or text.startswith("--", index):
            raise ValueError(
                "property_filter must be a single expression; remove the "
                f"statement separator or comment from {property_filter!r}"
            )
        index += 1

    if quote is not None:
        raise ValueError(
            f"property_filter has an unterminated quote: {property_filter!r}"
        )
    if depth:
        raise ValueError(
            f"property_filter leaves {depth} parenthesis/es unclosed: {property_filter!r}"
        )


def _reader_errors(reader: Any) -> tuple[type[BaseException], ...]:
    """Return the exception types a reader call may fail with recoverably.

    `pyramids-eo` is an optional extra, so its `ReaderError` cannot be imported
    at module load. It is **not** re-exported from `pyramids_eo.earthengine`
    either, so it is resolved from `pyramids_eo.errors` — looking only at the
    passed module would silently yield a tuple that never matches, and every
    recoverable refusal would escape as a crash.

    Transport and argument errors are included because a discovery round-trip
    can fail as either. `AuthenticationError` is deliberately absent: a
    credential problem must surface rather than silently downgrade the run.

    Args:
        reader: The imported `pyramids_eo.earthengine` module, consulted first
            in case a future release does re-export the error.

    Returns:
        The exception classes to treat as a recoverable reader failure.
    """
    errors: list[type[BaseException]] = [OSError, ValueError]
    candidates = [getattr(reader, "ReaderError", None)]
    try:
        from pyramids_eo.errors import ReaderError

        candidates.append(ReaderError)
    except ImportError:  # pragma: no cover - the extra is installed wherever this runs
        pass
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            errors.append(candidate)
    return tuple(dict.fromkeys(errors))


def _validate_filters(
    filters: Iterable[CollectionFilter] | None,
) -> tuple[CollectionFilter, ...]:
    """Normalise the constructor `filters` into a validated tuple.

    Args:
        filters: The raw `filters=` argument — `None`, or an iterable of
            `ee.ImageCollection -> ee.ImageCollection` callables (a
            one-shot generator is accepted and materialised). Order is
            preserved, so an *ordered* iterable is expected (a `set`
            applies in arbitrary order).

    Returns:
        The filters as a tuple (empty when `filters is None`).

    Raises:
        TypeError: If `filters` is a `str`, a bytes-like object
            (`bytes` / `bytearray` / `memoryview`), or a mapping; is not
            iterable; or contains a non-callable entry.
    """
    if filters is None:
        return ()
    if isinstance(
        filters, (str, bytes, bytearray, memoryview, Mapping)
    ) or not isinstance(filters, Iterable):
        raise TypeError(
            "filters must be an iterable of callables "
            "(ee.ImageCollection -> ee.ImageCollection) or None, got "
            f"{type(filters).__name__}"
        )
    collection_filters = tuple(filters)
    for image_filter in collection_filters:
        if not callable(image_filter):
            raise TypeError(
                "each entry in filters must be a callable "
                "ee.ImageCollection -> ee.ImageCollection, got "
                f"{type(image_filter).__name__}"
            )
    return collection_filters


class EedaiPlan(NamedTuple):
    """How the EEDAI reader should serve one request, or why it should not.

    Attributes:
        can_serve: Whether the reader takes this read at all.
        tile_size: Output pixels per tile side when the read is streamed, or
            `None` for a single pass (and when `can_serve` is `False`).
        tiles: How many tiles the streamed read is cut into; `1` for a single
            pass. Carried here so the exporter never re-derives it — a second
            derivation is free to disagree with the one that was routed on.
        reason: Why the reader declined, empty when it did not.
    """

    can_serve: bool
    tile_size: int | None
    tiles: int
    reason: str


class GEE(LazyClientMixin, AbstractDataSource):
    """Google Earth Engine data source.

    Args:
        start: Inclusive start date string (parsed with `fmt`).
        end: Inclusive end date string.
        variables: Mapping `{asset_id: [band, ...]}` — each `asset_id`
            must be a key of :attr:`Catalog.datasets` and each band a
            band of that dataset (see `src/earthlens/gee/catalog/`).
        lat_lim: `[lat_min, lat_max]` in degrees.
        lon_lim: `[lon_min, lon_max]` in degrees.
        temporal_resolution: How to composite over time — `"raw"` (one
            image: reduce the whole window), `"daily"`, `"monthly"`, or
            `"yearly"`. Defaults to `"raw"`.
        path: Output directory (created if absent). Defaults to the configured
            earthlens output directory (`set_output_dir()` /
            `EARTHLENS_DATA_DIR`); see `earthlens.config`.
        fmt: `strptime` format for `start` / `end`. Defaults to `"%Y-%m-%d"`.
        scale: Output pixel size in metres. If omitted, each dataset's
            nominal `spatial_resolution` is used.
        crs: Output CRS (EPSG code string). Defaults to `"EPSG:4326"`.
        reducer: Override the per-dataset `default_reducer` for the
            temporal composite (`mean` / `median` / `min` / `max` /
            `mode` / `mosaic` / `sum`). `None` (the default) uses each
            dataset's own `default_reducer`.
        export_via: How to get pixels out — `"url"` (synchronous
            `getDownloadURL`, capped at 32768 px per axis; the default),
            `"drive"` (asynchronous `ee.batch.Export.image.toDrive`;
            requires `drive_folder`), `"gcs"` (asynchronous
            `ee.batch.Export.image.toCloudStorage`; requires `gcs_bucket`),
            or `"asset"` (asynchronous `ee.batch.Export.image.toAsset`;
            requires `asset_id`).
        drive_folder: Google Drive folder name for `export_via="drive"`.
        gcs_bucket: Cloud Storage bucket name for `export_via="gcs"` (the
            service account needs `roles/storage.objectAdmin` on it).
        asset_id: Parent folder asset id for `export_via="asset"` (e.g.
            `"projects/my-project/assets/my-folder"`). Each export's asset
            is created at `<asset_id>/<prefix>`.
        region: Optional `GeoDataFrame` to clip to precisely; when given
            it supersedes the lat/lon bbox for the actual clip (the bbox
            is still used for the `"url"` size estimate).
        http_timeout: Timeout in seconds for the synchronous
            `getDownloadURL` HTTP request (`export_via="url"`). Defaults
            to 300 s.
        auto_split: For `export_via="url"`, when the estimated request
            exceeds Earth Engine's 32768-px per-axis cap, automatically
            split the AOI into tiles each within the cap, download each
            tile separately, and mosaic them back into a single GeoTIFF
            via `pyramids.dataset.merge.merge_rasters`. Defaults to
            `False` — the previous behaviour, which raises `ValueError`
            with an actionable message.
        discover_extent: When the catalog entry's `extent.end_date`
            (and/or `start_date`) is missing, fall back to an EE-side
            `reduceColumns(minMax)` over `system:time_start` to discover
            the collection's actual extent and clamp the request window
            to it. The discovered extent is cached per asset for the
            lifetime of the `GEE` instance. Defaults to `False` — the
            previous behaviour, which uses `now() + 1 day` as the upper
            bound for open-ended catalog entries.
        wait_for_export: For asynchronous sinks (`export_via="drive"` /
            `"gcs"` / `"asset"`), whether `download()` blocks until
            each task reaches a terminal state. Defaults to `True`
            (the historical behaviour — returns the destination
            string). When `False`, each task is started and
            `download()` returns a list of :class:`TaskInfo` objects
            so the caller can track them asynchronously via
            :mod:`earthlens.gee.jobs`. Ignored for `export_via="url"`,
            which is always synchronous.
        cloud_mask: Optional per-image mask `.map`-applied to every image
            in the stack *before* the reducer, so the composite is built
            from cloud-screened pixels — the usual way to get a clean
            optical mosaic. A callable `ee.Image -> ee.Image`; see
            :mod:`earthlens.gee.cloud_masks` (`landsat_sr` /
            `sentinel2_scl`). It runs before the band `select`, so it may
            read quality bands (`QA_PIXEL` / `SCL`) that are not listed in
            `variables`. Meant for image collections; on a static
            `ee_type="image"` dataset it is applied verbatim and a warning
            is logged (see :meth:`_build_collection`). Defaults to `None`
            (no masking).
        filters: Optional iterable of `ee.ImageCollection ->
            ee.ImageCollection` filters applied to the stack after the
            spatial / temporal filters (`filterBounds`, and `filterDate`
            for image collections) and before the `cloud_mask` and
            reducer — e.g. a metadata cloud-cover cap. Each entry takes
            the collection and returns it; wrap the
            :mod:`earthlens.gee.filters` helpers (`by_cloud_cover_lte` /
            `by_property_in` / ...), whose first argument is the
            collection, with `functools.partial` or a lambda —
            `partial(by_cloud_cover_lte, max_pct=60)`. Applied left to
            right, so pass an *ordered* iterable (a `set` would apply in
            arbitrary order); like `cloud_mask`, meant for image
            collections. Defaults to `None` (no extra filters).
        engine: Which layer materialises the pixels for `export_via="url"`.
            `"auto"` (the default) uses the pyramids-eo EEDAI reader when
            nothing server-side has to shape the image — no `cloud_mask`,
            no `filters` — and the `[eedai]` extra is installed. That covers
            a raw read of a materialised `ee_type="image"` asset *and* an
            `ee_type="image_collection"` composited client-side per time
            bucket, in `crs="EPSG:4326"` or any metre-based projected CRS.
            A collection is additionally sized before it is served: too many
            scenes, too large a stack, or the `mosaic` reducer (whose
            client-side meaning differs from Earth Engine's last-wins) sends
            it back to Earth Engine. It falls back to Earth Engine's
            `getDownloadURL` otherwise. `"ee"` always uses `getDownloadURL`
            (the historical behaviour). `"eedai"` forces the reader and
            raises if the request is not eligible. The EEDAI path reads
            pixels straight from the asset, so Earth Engine's 32768-px
            synchronous cap does not apply and `auto_split` is unnecessary —
            a window too large to materialise is streamed to disk in tiles
            and mosaicked. It cannot run server-side compute, which is why
            composited requests stay on Earth Engine. Ignored for the
            asynchronous `"drive"` / `"gcs"` / `"asset"` sinks, which are
            Earth Engine-only.

            The two engines do not produce byte-identical rasters. Earth
            Engine reads `scale` in a geographic CRS as a uniform
            degree-equivalent, while the EEDAI grid is sized for square
            metres on the ground, so away from the equator the column counts
            differ; and the reader downsamples locally (nearest by default)
            where Earth Engine aggregates server-side. The AOI, CRS and
            values agree — the sampling does not. `"eedai"` still needs the
            `[gee]` extra and Earth Engine credentials: the request is built
            through `ee` before the pixels are fetched.
        cog: Write the EEDAI path's raster as a Cloud Optimized GeoTIFF
            (tiled, with overviews) via `Dataset.cog.to_cog` instead of a
            plain GeoTIFF. Applies only to the EEDAI path — the Earth
            Engine `getDownloadURL` and batch-export sinks are unaffected.
            Defaults to `False`.
        resample: Resampling kernel the EEDAI reader warps the native grid
            with — `"nearest"` (the default), `"average"`, `"bilinear"`, … .
            This path always warps from the asset's native resolution to the
            requested `scale`, so for continuous fields (elevation,
            temperature, reflectance) being read coarser than native,
            `"average"` is closer to Earth Engine's server-side aggregation
            than the point-sampling default; keep `"nearest"` for
            categorical data such as land cover. Ignored on the Earth Engine
            path, which resamples server-side.
        property_filter: An OGR attribute-filter string on a collection's own
            scene properties (e.g. `"CLOUDY_PIXEL_PERCENTAGE < 20"`), narrowing
            which scenes the EEDAI collection composite reads. earthlens's
            `filters` are Earth Engine closures with no string form, so this is
            a separate, reader-only knob; it applies only to the EEDAI
            `image_collection` path and is ignored (with a warning) for a single
            image or an Earth Engine-served request. **Build it in code, never
            from untrusted input**: it is interpolated verbatim into the
            reader's attribute filter without escaping, so a crafted fragment
            can neutralise the time and space clauses it is combined with. Only
            obvious malformations (unbalanced quotes or parentheses, a statement
            separator, a SQL comment) are rejected here. Defaults to `None`.

    Credentials are not constructor arguments — the constructor describes
    only what to fetch. Supply them at the authentication step:
    :meth:`authenticate` accepts `service_account=` / `service_key=` /
    `project=`, each falling back to the `GEE_SERVICE_ACCOUNT` /
    `GEE_SERVICE_KEY` / `GEE_PROJECT` environment variable when omitted.
    `download()` opens the connection lazily (resolving the same way) if
    `authenticate()` was never called.

    Raises:
        AuthenticationError: If Earth Engine cannot be initialised
            (missing/invalid key, unregistered project, missing IAM role).
        ValueError: At construction for a bad `export_via` (or `"drive"`
            without `drive_folder` / `"gcs"` without `gcs_bucket` /
            `"asset"` without `asset_id`); from the parent on a bad date
            range; from :meth:`_check_input_dates` on an unknown
            `temporal_resolution`; from :meth:`_api` on a missing scale
            or an oversized `"url"` request (unless `auto_split=True`);
            from :meth:`_download_dataset` on an unknown asset id or band.
        TypeError: At construction when `cloud_mask` is not callable, or
            `filters` is a `str` / bytes-like / mapping / non-iterable or
            contains a non-callable entry.
        NotImplementedError: From :meth:`download` when `aggregate=` is
            passed (not yet supported).
        RuntimeError: From :meth:`_api` if a `"drive"` / `"gcs"` export
            task does not complete.

    Examples:
        - Authenticate against a service account, then download SRTM over a small bbox:
            ```python
            >>> from earthlens.gee import GEE  # doctest: +SKIP
            >>> gee = GEE(  # doctest: +SKIP
            ...     start="2000-02-11", end="2000-02-12",
            ...     variables={"USGS/SRTMGL1_003": ["elevation"]},
            ...     lat_lim=[29.9, 30.0], lon_lim=[31.2, 31.3],
            ...     path="data/gee",
            ... )
            >>> paths = gee.authenticate(  # doctest: +SKIP
            ...     service_account="sa@my-project.iam.gserviceaccount.com",
            ...     service_key="/path/to/key.json",
            ... ).download()

            ```
        - Read the same raw asset through the pyramids-eo EEDAI reader and write
          a Cloud Optimized GeoTIFF (no 32768-px cap, no `auto_split`):
            ```python
            >>> from earthlens.gee import GEE  # doctest: +SKIP
            >>> gee = GEE(  # doctest: +SKIP
            ...     start="2000-02-11", end="2000-02-12",
            ...     variables={"USGS/SRTMGL1_003": ["elevation"]},
            ...     lat_lim=[29.9, 30.0], lon_lim=[31.2, 31.3],
            ...     path="data/gee", scale=90,
            ...     engine="eedai", cog=True,
            ... )
            >>> paths = gee.authenticate().download()  # doctest: +SKIP
            >>> paths[0].name  # doctest: +SKIP
            'USGS_SRTMGL1_003_elevation_20000211.tif'

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = (
        "the reducer is not wired for this backend yet (planned — see the GEE "
        "plan task M3)"
    )

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    @property
    def catalog(self):
        """The bundled GEE :class:`~earthlens.gee.Catalog` (alias of `_catalog`)."""
        return self._catalog

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        scale: float | None = None,
        crs: str = "EPSG:4326",
        reducer: str | None = None,
        export_via: Literal["url", "drive", "gcs", "asset"] = "url",
        drive_folder: str | None = None,
        gcs_bucket: str | None = None,
        asset_id: str | None = None,
        region: GeoDataFrame | None = None,
        http_timeout: float | None = None,
        auto_split: bool = False,
        discover_extent: bool = False,
        wait_for_export: bool = True,
        cloud_mask: CloudMask | None = None,
        filters: Iterable[CollectionFilter] | None = None,
        engine: Literal["auto", "ee", "eedai"] = "auto",
        cog: bool = False,
        resample: str = "nearest",
        property_filter: str | None = None,
    ):
        # Validate the cheap (no-I/O) config first so user typos surface
        # before the ~3.3 s cold-cache catalog parse below.
        if export_via not in {"url", "drive", "gcs", "asset"}:
            raise ValueError(
                f"export_via must be 'url', 'drive', 'gcs', or 'asset', "
                f"got {export_via!r}"
            )
        if property_filter is not None:
            _validate_property_filter(property_filter)
        if export_via == "drive" and not drive_folder:
            raise ValueError("export_via='drive' requires drive_folder=")
        if export_via == "gcs" and not gcs_bucket:
            raise ValueError("export_via='gcs' requires gcs_bucket=")
        if export_via == "asset" and not asset_id:
            raise ValueError(
                "export_via='asset' requires asset_id= (the parent folder "
                "asset, e.g. 'projects/my-project/assets/my-folder')"
            )
        if cloud_mask is not None and not callable(cloud_mask):
            raise TypeError(
                "cloud_mask must be a callable ee.Image -> ee.Image (or None), "
                f"got {type(cloud_mask).__name__}"
            )
        collection_filters = _validate_filters(filters)
        if engine not in _ENGINES:
            raise ValueError(
                f"engine must be one of {sorted(_ENGINES)}, got {engine!r}"
            )
        _validate_pure_config(start, end, temporal_resolution, fmt)

        # These must be set before `super().__init__` runs, because the
        # parent constructor immediately calls `self._initialize()` (and
        # `_create_grid` / `_check_input_dates`), which read them.
        self._catalog = Catalog()
        # Credentials are resolved at authenticate()/first-client-access time
        # (explicitly or from the GEE_SERVICE_ACCOUNT / GEE_SERVICE_KEY /
        # GEE_PROJECT environment variables), not at construction.
        self._service_account: str | None = None
        self._service_key: str | None = None
        self._project: str | None = None
        self.project: str | None = None
        self.scale = scale
        self.crs = crs
        self.reducer = reducer
        self.export_via = export_via
        self.drive_folder = drive_folder
        self.gcs_bucket = gcs_bucket
        self.asset_id = asset_id
        self.region = region
        self.http_timeout = (
            float(http_timeout) if http_timeout is not None else _DEFAULT_HTTP_TIMEOUT_S
        )
        self.auto_split = bool(auto_split)
        self.discover_extent = bool(discover_extent)
        self.wait_for_export = bool(wait_for_export)
        #: The per-image `cloud_mask` hook (or `None`), `.map`-applied
        #: before the reducer in :meth:`_build_collection`.
        self.cloud_mask = cloud_mask
        #: The validated `filters` as a tuple (empty when none were given),
        #: applied left to right in :meth:`_build_collection`.
        self.filters: tuple[CollectionFilter, ...] = collection_filters
        #: Which layer materialises the pixels: `"auto"` (the pyramids-eo
        #: EEDAI reader when the request is eligible and installed, else
        #: Earth Engine), `"ee"`, or `"eedai"`.
        self.engine = engine
        #: Write the EEDAI path's output as a Cloud Optimized GeoTIFF.
        self.cog = bool(cog)
        #: Resampling kernel the EEDAI reader warps with (`nearest` by default).
        self.resample = resample
        #: OGR attribute-filter string narrowing collection scenes on the EEDAI
        #: path (e.g. `"CLOUDY_PIXEL_PERCENTAGE < 20"`); Earth Engine ignores it.
        self.property_filter = property_filter
        self._ee_geometry: Any = None  # lazily built in `_ee_region`
        self._eedai_credential: Any = None  # lazily built in `_eedai_credentials`
        self._cog_warned = False  # one-shot guard for the `cog=` notice
        self._property_filter_warned: set[str] = set()  # per-dataset filter notice

        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _resolve_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Resolve credentials from explicit values, then the environment.

        Each credential piece falls back to its environment variable when
        not set explicitly (via :meth:`authenticate`): `GEE_SERVICE_ACCOUNT`,
        `GEE_SERVICE_KEY`, `GEE_PROJECT`.

        Returns:
            tuple: `(service_account, service_key, project)`, each `None`
                when neither an explicit value nor its env var is set.

        Examples:
            - Explicit values (set by :meth:`authenticate`) are returned as-is:
                ```python
                >>> import tempfile
                >>> from earthlens.gee import GEE
                >>> gee = GEE(
                ...     start="2000-02-11", end="2000-02-12",
                ...     variables={"USGS/SRTMGL1_003": ["elevation"]},
                ...     lat_lim=[29.9, 30.0], lon_lim=[31.2, 31.3],
                ...     path=tempfile.mkdtemp(),
                ... )
                >>> gee._service_account = "sa@demo.iam.gserviceaccount.com"
                >>> gee._service_key = "/path/to/key.json"
                >>> gee._project = "demo-project"
                >>> gee._resolve_credentials()
                ('sa@demo.iam.gserviceaccount.com', '/path/to/key.json', 'demo-project')

                ```
        """
        service_account = self._service_account or os.environ.get("GEE_SERVICE_ACCOUNT")
        service_key = self._service_key or os.environ.get("GEE_SERVICE_KEY")
        project = self._project or os.environ.get("GEE_PROJECT")
        return service_account, service_key, project

    def authenticate(
        self,
        service_account: str | None = None,
        service_key: str | None = None,
        project: str | None = None,
    ) -> GEE:
        """Resolve credentials and open the Earth Engine connection.

        The explicit, fail-fast credential step. Pass `service_account=`
        + `service_key=` (and optionally `project=`) to authenticate with
        a service-account key; omit a value to read its `GEE_SERVICE_ACCOUNT`
        / `GEE_SERVICE_KEY` / `GEE_PROJECT` environment variable instead.
        Opening the connection (which `download()` also does lazily if you
        never call this) validates the credentials against Earth Engine.

        Args:
            service_account: Service-account email. When `None`, the
                `GEE_SERVICE_ACCOUNT` environment variable is read.
            service_key: Path to the service-account JSON key file, or the
                JSON content as a string. When `None`, the `GEE_SERVICE_KEY`
                environment variable is read.
            project: Cloud project id to scope Earth Engine calls to. When
                `None`, the `GEE_PROJECT` environment variable is read (or
                the project is taken from the key's `project_id`).

        Returns:
            The backend instance, so it chains
            `EarthLens(...).authenticate(...).download()`.

        Raises:
            AuthenticationError: If no service-account pair and no project
                can be resolved, or Earth Engine rejects the credentials.

        Examples:
            - Authenticate with a service-account key, then download (live;
              skipped here):
                ```python
                >>> from earthlens.gee import GEE  # doctest: +SKIP
                >>> GEE(  # doctest: +SKIP
                ...     start="2000-02-11", end="2000-02-12",
                ...     variables={"USGS/SRTMGL1_003": ["elevation"]},
                ...     lat_lim=[29.9, 30.0], lon_lim=[31.2, 31.3], path="data/gee",
                ... ).authenticate(
                ...     service_account="sa@my-project.iam.gserviceaccount.com",
                ...     service_key="/path/to/key.json",
                ... ).download()

                ```
            - Resolve the same credentials from the environment instead of
              passing them (live; skipped here):
                ```python
                >>> import os  # doctest: +SKIP
                >>> os.environ["GEE_SERVICE_ACCOUNT"] = "sa@my-project.iam.gserviceaccount.com"
                >>> os.environ["GEE_SERVICE_KEY"] = "/path/to/key.json"
                >>> GEE(  # doctest: +SKIP
                ...     start="2000-02-11", end="2000-02-12",
                ...     variables={"USGS/SRTMGL1_003": ["elevation"]},
                ...     lat_lim=[29.9, 30.0], lon_lim=[31.2, 31.3], path="data/gee",
                ... ).authenticate().download()

                ```
        """
        if service_account is not None:
            self._service_account = service_account
        if service_key is not None:
            self._service_key = service_key
        if project is not None:
            self._project = project
        # Re-authenticating may switch identity, so the reader's cached
        # credential must not outlive the values it was built from.
        self._eedai_credential = None
        # LazyClientMixin: first access to `client` runs `_open_client` (auth).
        _ = self.client
        return self

    def _open_client(self) -> Any:
        """Authenticate and initialise the Earth Engine connection (lazily).

        Resolves the credentials (explicit values from :meth:`authenticate`,
        else the `GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY` / `GEE_PROJECT`
        environment variables), then uses a service-account key when a
        `service_account` + `service_key` pair is available (via
        :class:`EarthEngineAuth`); otherwise runs `ee.Authenticate()` and
        `ee.Initialize(project=...)` against the resolved `project`. The
        `ee.Authenticate()` flow is interactive — it opens a browser and
        waits for the user to paste a token, so on a headless box (CI,
        Docker, remote shell) it will hang or fail with whatever the EE
        SDK emits natively; use service-account auth for non-interactive
        use. The resolved project id is stored on :attr:`project`. Called
        by :attr:`~earthlens.base.LazyClientMixin.client` on first use.

        Returns:
            The `ee` module (cached as `self.client`).

        Raises:
            AuthenticationError: If no service-account pair and no project
                can be resolved, the credentials are invalid, the project
                is not registered for Earth Engine, or the service account
                lacks the required IAM role on it.
        """
        service_account, service_key, project = self._resolve_credentials()
        if not (service_account and service_key) and not project:
            raise AuthenticationError(
                "the GEE backend needs either service_account + service_key, "
                "or an explicit project=, supplied to authenticate(...) or via "
                "the GEE_SERVICE_ACCOUNT / GEE_SERVICE_KEY / GEE_PROJECT "
                "environment variables. See "
                "https://developers.google.com/earth-engine/guides/service_account."
            )
        if service_account and service_key:
            self.project = EarthEngineAuth.initialize(
                service_account, service_key, project
            )
            return ee
        try:
            ee.Authenticate()
            ee.Initialize(project=project)
        except ee.EEException as exc:
            message = str(exc)
            if "not registered to use Earth Engine" in message:
                raise AuthenticationError(
                    f"Cloud project {project!r} is not registered to use "
                    "Earth Engine. Register it at "
                    "https://code.earthengine.google.com/register, then retry."
                ) from exc
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project {project!r}: {message}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project {project!r}: {exc}"
            ) from exc
        self.project = project
        return ee

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date range and produce the per-bucket date index.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: `"raw"` (one bucket spanning the whole
                window), `"daily"` (`freq="D"`), `"monthly"` (`"MS"`),
                or `"yearly"` (`"YS"`).
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: `start_date`, `end_date`, `resolution` (the
            string passed in), and `dates` — a :class:`pandas.DatetimeIndex`
            with one entry per time bucket (a single entry for `"raw"`).

        Raises:
            ValueError: If `temporal_resolution` is not one of `"raw"`,
                `"daily"`, `"monthly"`, `"yearly"`, or if `start > end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        if temporal_resolution == "raw":
            dates = pd.DatetimeIndex([start_dt])
        elif temporal_resolution in _RESOLUTION_FREQ:
            dates = date_windows(
                start_dt, end_dt, _RESOLUTION_FREQ[temporal_resolution]
            )
        else:
            raise ValueError(
                "temporal_resolution must be 'raw', 'daily', 'monthly', or "
                f"'yearly', got {temporal_resolution!r}"
            )
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=dates,
        )

    def download(self, progress_bar: bool = True) -> list[Path | str | TaskInfo]:
        """Download every requested band-set of every requested dataset.

        Args:
            progress_bar: Show a per-bucket `tqdm` bar. Defaults to `True`.

        Returns:
            One entry per `(dataset, band-set, time-bucket)`. The
            shape depends on the sink:

            * `export_via="url"` — `pathlib.Path` to the
                written GeoTIFF (always synchronous).
            * `export_via="drive"` / `"gcs"` / `"asset"` with the
                default `wait_for_export=True` — destination string
                (`"drive://<folder>/<prefix>"` / `"gs://<bucket>/<prefix>"` /
                `"ee://<asset_id>/<prefix>"`), populated only once
                the task reaches `COMPLETED`.
            * `export_via="drive"` / `"gcs"` / `"asset"` with
                `wait_for_export=False` — `TaskInfo` captured
                at submission time; follow up via
                `earthlens.gee.jobs` (`get_task_status`,
                `wait_for_task_id`, etc.).

        Raises:
            ValueError: On an unknown asset id, an unknown band, or an
                oversized `"url"` request (see :meth:`_api`).
            RuntimeError: If a `"drive"` / `"gcs"` / `"asset"` export
                task fails. Only raised when `wait_for_export=True`;
                in the non-blocking mode the caller handles failures
                themselves via `wait_for_task_id`.

        Examples:
            - Download one band, one image (needs network + credentials):
                ```python
                >>> gee = GEE(  # doctest: +SKIP
                ...     start="2020-06-01", end="2020-06-30",
                ...     temporal_resolution="monthly",
                ...     variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
                ...     lat_lim=[29.0, 30.0], lon_lim=[31.0, 32.0],
                ...     path="data/gee", scale=5566,
                ... )
                >>> gee.authenticate(  # doctest: +SKIP
                ...     service_account="sa@p.iam.gserviceaccount.com",
                ...     service_key="/path/to/key.json",
                ... )
                >>> paths = gee.download()  # doctest: +SKIP
                >>> [p.name for p in paths]  # doctest: +SKIP
                ['UCSB-CHG_CHIRPS_DAILY_precipitation_20200601.tif']

                ```
            - `aggregate=` is not yet supported and is rejected up front:
                ```python
                >>> gee = GEE(  # doctest: +SKIP
                ...     start="2020-06-01", end="2020-06-01",
                ...     variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
                ...     lat_lim=[29.0, 30.0], lon_lim=[31.0, 32.0],
                ...     scale=5566,
                ... )
                >>> gee.download(aggregate=object())  # doctest: +SKIP
                Traceback (most recent call last):
                    ...
                NotImplementedError: aggregate= is not yet supported ...

                ```

        See Also:
            earthlens.gee.Catalog: Resolves the `{asset_id: [band, ...]}`
                request against `src/earthlens/gee/catalog/`.
            earthlens.gee.auth.EarthEngineAuth: Performs the one-time
                `ee.Initialize` used by :meth:`_open_client`.
        """
        # Trigger the lazy Earth Engine auth/init before any `ee` call.
        _ = self.client
        self._cog_warned = False  # the cog= notice is once per run, not per object
        self._property_filter_warned = set()  # same, for the property_filter notice
        outputs: list[Path | str | TaskInfo] = []
        assert isinstance(
            self.vars, dict
        )  # GEE always uses the {asset_id: [band]} form
        for asset_id, bands in self.vars.items():
            outputs.extend(self._download_dataset(asset_id, list(bands), progress_bar))
        return outputs

    def _download_dataset(
        self, asset_id: str, bands: list[str], progress_bar: bool = True
    ) -> list[Path | str | TaskInfo]:
        """Download one dataset's requested bands across the time buckets.

        Validates `asset_id` and every band against the catalog, clamps
        the request window to the dataset's published extent, builds the
        filtered collection, composites it per time bucket, and writes
        each bucket via :meth:`_api`.

        Args:
            asset_id: An Earth Engine asset id present in the catalog.
            bands: Band ids of that dataset to download.
            progress_bar: Show a `tqdm` bar over the time buckets.

        Returns:
            The list of GeoTIFF paths written for this dataset (possibly
            empty if the request window does not overlap the dataset's
            extent).

        Raises:
            ValueError: If `asset_id` or any band is not in the catalog,
                or if a write fails the size guard (see :meth:`_api`).
        """
        var_info = self._catalog.get_dataset(asset_id)
        for band in bands:
            var_info.get_band(band)  # raises ValueError with a suggestion

        start, end = self._clamp_window_to_extent(var_info)
        if start is None:
            logger.warning(
                f"{asset_id}: request window does not overlap the dataset's "
                f"extent ({var_info.extent.start_date}..{var_info.extent.end_date}); "
                "skipping."
            )
            return []

        assert end is not None  # _clamp_window_to_extent returns both bounds or neither
        collection = self._build_collection(var_info, bands, start, end)
        buckets = list(self._composite(collection, var_info, start, end))
        iterator: Iterable = buckets
        if progress_bar:
            iterator = tqdm(buckets, desc=f"{asset_id} [{','.join(bands)}]", unit="img")
        return [
            self._api(image, var_info, bands, when, bucket_start, bucket_end)
            for when, image, bucket_start, bucket_end in iterator
        ]

    def _build_collection(
        self, var_info: Dataset, bands: list[str], start: dt.datetime, end: dt.datetime
    ):
        """Build the filtered, cloud-masked, band-selected `ee.ImageCollection`.

        For an `ee_type="image"` dataset the single `ee.Image` is wrapped
        in a one-element collection so the rest of the pipeline is
        uniform. `filterDate` uses a half-open `[start, end]` window
        (Earth Engine convention); the `end` passed here is already
        bumped by one day by :meth:`_clamp_window_to_extent` so the
        user's inclusive end date is covered.

        The pipeline is `filterDate` (image collections only) →
        `filterBounds` → the constructor `filters` (left to right) → the
        per-image `cloud_mask` (`.map`) → `select(bands)`. The
        `cloud_mask` runs *before* `select` on purpose: an optical mask
        reads a quality band (`QA_PIXEL` / `SCL`) that the user's `bands`
        usually omit, so selecting the requested bands first would strip
        it.

        `filters` and `cloud_mask` are meant for image collections. On a
        static `ee_type="image"` dataset they are still applied verbatim
        (and a `logger.warning` is emitted): a metadata filter can drop
        the single wrapped image and empty the collection, while a mask
        reading a band the asset lacks fails when the graph is computed —
        either way it surfaces later as an opaque Earth Engine error at
        download time rather than here.

        Args:
            var_info: The catalog entry.
            bands: Band ids to `.select(...)`.
            start: Inclusive window start (clamped).
            end: Exclusive window end (clamped, already +1 day).

        Returns:
            The `ee.ImageCollection`.
        """
        if var_info.is_image_collection:
            collection = ee.ImageCollection(var_info.id).filterDate(
                start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            )
        else:
            # A static image: no temporal filtering (the asset may not
            # carry a `system:time_start` inside the request window).
            collection = ee.ImageCollection([ee.Image(var_info.id)])
            if self.filters or self.cloud_mask is not None:
                logger.warning(
                    f"filters / cloud_mask were set but {var_info.id!r} is a "
                    "static single-image dataset (ee_type='image'); they are "
                    "applied verbatim — a metadata filter can empty the "
                    "collection, and a mask reading an absent band fails when "
                    "the graph is computed; either way it surfaces as an opaque "
                    "Earth Engine error at download time."
                )
        collection = collection.filterBounds(self._ee_region())
        for image_filter in self.filters:
            collection = image_filter(collection)
        if self.cloud_mask is not None:
            collection = collection.map(self.cloud_mask)
        return collection.select(list(bands))

    def _composite(
        self, collection, var_info: Dataset, start: dt.datetime, end: dt.datetime
    ):
        """Yield one `ee.Image` per time bucket.

        For `temporal_resolution="raw"` (and for static `ee_type="image"`
        datasets) there is a single bucket spanning the whole clamped
        window. Otherwise the window is split into daily / monthly /
        yearly buckets and each is collapsed with the dataset's
        `default_reducer` (or the constructor `reducer` override).

        Args:
            collection: The filtered `ee.ImageCollection` from
                :meth:`_build_collection`.
            var_info: The catalog entry (its `default_reducer`).
            start: Inclusive window start (clamped).
            end: Exclusive window end (clamped, already +1 day).

        Yields:
            `(timestamp, ee.Image, bucket_start, bucket_end)` tuples —
            `timestamp` is the bucket start (used in the filename), and
            `(bucket_start, bucket_end)` is the bucket's half-open date window
            (both :class:`datetime.datetime`), which the EEDAI collection path
            re-composites client-side.
        """
        reducer = self.reducer or var_info.default_reducer
        if self.temporal_resolution == "raw" or not var_info.is_image_collection:
            yield start, reduce_collection(collection, reducer), start, end
            return
        freq = _RESOLUTION_FREQ[self.temporal_resolution]
        bucket_starts = date_windows(start, end, freq, inclusive="left")
        for i, bucket_start in enumerate(bucket_starts):
            bucket_end = (
                bucket_starts[i + 1]
                if i + 1 < len(bucket_starts)
                else pd.Timestamp(end)
            )
            window = collection.filterDate(
                bucket_start.strftime("%Y-%m-%d"), bucket_end.strftime("%Y-%m-%d")
            )
            yield (
                bucket_start.to_pydatetime(),
                reduce_collection(window, reducer),
                bucket_start.to_pydatetime(),
                bucket_end.to_pydatetime(),
            )

    def _api(
        self,
        image,
        var_info: Dataset,
        bands: list[str],
        when: dt.datetime,
        bucket_start: dt.datetime,
        bucket_end: dt.datetime,
    ) -> Path | str | TaskInfo:
        """Export one composited `ee.Image` via the configured `export_via`.

        For `export_via="url"`: estimate the request's pixel dimensions
        from the bbox and `scale`; if either axis exceeds Earth Engine's
        32768-px synchronous limit, either auto-split + mosaic via
        pyramids (when `auto_split=True`) or raise a `ValueError`
        pointing the user at a coarser `scale`, a smaller bbox,
        `export_via="drive"`, or `auto_split=True`. Otherwise request a
        GeoTIFF via `getDownloadURL` and stream it to disk as
        `<asset-slug>_<bands>_<YYYYMMDD>.tif`. For `export_via="drive"` /
        `"gcs"` / `"asset"`: queue an
        `ee.batch.Export.image.to{Drive,CloudStorage,Asset}` task, poll
        it to completion (no synchronous size cap, just `maxPixels`),
        and return a destination string — for Drive / GCS the file is
        left in the destination for the caller to pull; for `"asset"`
        a new EE asset is created at `<asset_id>/<prefix>`.

        A raw, no-compute request may instead be served by the pyramids-eo
        EEDAI reader — see :meth:`_use_eedai` for when, and
        :meth:`_export_via_eedai` for what that path does. The composited
        `image` is then unused: the reader materialises the asset's own
        pixels.

        Args:
            image: The `ee.Image` to export (unused on the EEDAI path).
            var_info: The catalog entry (for the asset slug and the
                fallback `spatial_resolution`).
            bands: The band ids in `image` (used in the filename / prefix).
            when: The bucket timestamp (used in the filename / prefix).
            bucket_start: Inclusive start of this bucket's date window; the
                EEDAI collection path re-composites it client-side.
            bucket_end: Exclusive end of this bucket's date window.

        Returns:
            For `"url"`: the :class:`pathlib.Path` of the written GeoTIFF.
            For `"drive"` / `"gcs"` / `"asset"`: a destination string
            (`"drive://<folder>/<prefix>"` / `"gs://<bucket>/<prefix>"` /
            `"ee://<asset_id>/<prefix>"`).

        Raises:
            ValueError: If no output scale can be resolved; for `"url"` with
                `auto_split=False`, when the estimated request exceeds the
                32768-px limit; or, for a forced `engine="eedai"`, when the
                request cannot be served by the reader.
            AuthenticationError: If the EEDAI path cannot build credentials.
            ImportError: If `engine="eedai"` is forced without the `[eedai]`
                extra installed.
            RuntimeError: If Earth Engine returns a zip instead of a
                GeoTIFF (`"url"`), or a `"drive"` / `"gcs"` / `"asset"`
                export task does not complete.
        """
        scale = self.scale or var_info.spatial_resolution
        if scale is None:
            raise ValueError(
                f"no output scale for {var_info.id}: pass scale= (metres) to "
                "GEE(...) — the catalog has no nominal spatial_resolution for it."
            )
        prefix = f"{slug_asset_id(var_info.id)}_{'-'.join(bands)}_{when:%Y%m%d}"
        if self.export_via == "url":
            # An empty request is not one band: upstream opens every band the
            # asset has, so budget for that rather than under-counting.
            use_reader, plan = self._use_eedai(
                var_info,
                max(len(bands) or len(var_info.bands), 1),
                bucket_start,
                bucket_end,
            )
            if use_reader:
                assert plan is not None  # a yes always carries its plan
                try:
                    return self._export_via_eedai(
                        var_info,
                        bands,
                        float(scale),
                        prefix,
                        plan,
                        bucket_start,
                        bucket_end,
                    )
                except _reader_errors(import_earthengine_reader()) as exc:
                    # The reader can still refuse after routing has committed:
                    # a band set spanning resolution groups is refused by the
                    # collection reader although the single-image one handles
                    # it, and only upstream knows that. Under a forced engine
                    # that refusal is the answer; under `auto` this is a request
                    # Earth Engine can serve, so falling back keeps the contract
                    # that `auto` routes rather than fails.
                    if self.engine == "eedai":
                        raise
                    logger.warning(
                        f"the EEDAI reader could not serve {var_info.id} "
                        f"({exc}); falling back to Earth Engine for this bucket."
                    )
        self._warn_cog_ignored(var_info)
        self._warn_property_filter_ignored(var_info)
        # Only the Earth Engine paths need the `ee.Geometry`; the reader clips
        # to its own bbox / cutline.
        region = self._ee_region()
        if self.export_via == "url":
            return self._export_via_url(image, var_info, float(scale), region, prefix)
        return self._export_via_batch(image, float(scale), region, prefix)

    def _export_via_url(
        self, image, var_info: Dataset, scale: float, region, prefix: str
    ) -> Path:
        """Fetch a GeoTIFF from `image.getDownloadURL`; enforce the 32768-px cap.

        Earth Engine returns a single GeoTIFF when one band is exported and
        a zip archive of per-band GeoTIFFs when several are. Both shapes
        are routed through pyramids: single tifs via :meth:`Dataset.from_bytes`
        (writes the in-memory body to a `/vsimem/` path then materialises it
        on disk), zips via :meth:`Dataset.from_archive` (chained `/vsizip/`,
        merging members into one multi-band tif).

        Oversized AOIs (either axis above :data:`EE_MAX_DIMENSION` px at
        `scale`) take one of two paths: when `auto_split=True` was passed
        to the constructor, the bbox is tiled, each tile is downloaded
        individually, and the tiles are mosaicked into one GeoTIFF via
        :func:`pyramids.dataset.merge.merge_rasters`; otherwise a
        `ValueError` is raised with a coarser-scale / smaller-bbox /
        `export_via="drive"` hint.
        """
        width_px, height_px = self.space.estimate_pixel_dims(scale)
        if max(width_px, height_px) > EE_MAX_DIMENSION:
            if self.auto_split:
                return self._auto_split_and_download(image, var_info, scale, prefix)
            raise ValueError(
                f"{var_info.id}: the requested AOI at scale={scale} m is about "
                f"{width_px}x{height_px} px, over Earth Engine's "
                f"{EE_MAX_DIMENSION}-px per-axis limit for synchronous downloads. "
                "Use a coarser scale, a smaller bbox, export_via='drive', or "
                "auto_split=True."
            )
        return self._download_one_url_tile(image, region, scale, prefix)

    def _eedai_eligible(self, var_info: Dataset) -> bool:
        """Return whether this request is a raw read the EEDAI reader can serve.

        The pyramids-eo reader materialises pixels from a real asset id; it
        cannot execute an Earth Engine computation graph. So it can only
        stand in for `getDownloadURL` when nothing server-side shapes the
        image: a single materialised `ee_type="image"` asset, no per-image
        `cloud_mask`, and no collection `filters`. The asynchronous sinks
        are Earth Engine-only.

        A single `ee_type="image"` asset is read directly; an
        `ee_type="image_collection"` is composited client-side by the reader
        (a reducer over a date window), which is why a `cloud_mask` or
        `filters` — server-side shaping — still disqualifies either.

        The output CRS may be `"EPSG:4326"` or a metre-based projected CRS.
        The reader reads `bbox` in the *target* CRS, so this backend reprojects
        its lat/lon AOI into that CRS first (see :meth:`_eedai_window`); a
        geographic CRS other than EPSG:4326, or a projected one whose axis is
        not in metres, is not sized correctly by the metre `scale` and stays on
        Earth Engine.

        Args:
            var_info: The catalog entry for the dataset being fetched.

        Returns:
            `True` when the request is a raw, no-compute read.
        """
        return (
            self.export_via == "url"
            and self.cloud_mask is None
            and not self.filters
            and var_info.ee_type in ("image", "image_collection")
            and self._eedai_crs_supported()
        )

    def _eedai_crs_supported(self) -> bool:
        """Return whether the reader can serve pixels in this backend's `crs`.

        Supported: `"EPSG:4326"` (the lat/lon AOI is passed through), and any
        projected CRS whose axis unit is metres (the metre `scale` then sizes
        the grid directly). A non-4326 geographic CRS or a non-metre projected
        CRS is declined, because the metre `scale` would mis-size its grid.

        Returns:
            `True` when a read in `self.crs` can be sized correctly.
        """
        if self.crs.upper() == _EEDAI_NATIVE_CRS:
            return True
        try:
            from pyproj import CRS

            crs = CRS.from_user_input(self.crs)
        except Exception:  # noqa: BLE001 - an unparseable CRS is simply not supported
            return False
        units = {axis.unit_name.lower() for axis in crs.axis_info}
        return bool(crs.is_projected and units & {"metre", "meter", "m"})

    def _bbox_to_output_crs(
        self, latlon_bbox: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        """Reproject a lat/lon AOI envelope into the output CRS.

        Args:
            latlon_bbox: `(min_lon, min_lat, max_lon, max_lat)` in EPSG:4326.

        Returns:
            The envelope in `self.crs`; the input is returned unchanged when the
            output CRS is already EPSG:4326.
        """
        if self.crs.upper() == _EEDAI_NATIVE_CRS:
            return latlon_bbox
        from pyproj import Transformer

        transformer = Transformer.from_crs(_EEDAI_NATIVE_CRS, self.crs, always_xy=True)
        # transform_bounds densifies the edges, so a reprojected rectangle that
        # bows still bounds the whole AOI rather than only its corners.
        #
        # Known asymmetry for a *large* projected AOI: upstream converts this
        # window back to EPSG:4326 with a four-corner transform, which
        # under-estimates the reach of an edge that bows outward. Measured on a
        # 10 deg x 10 deg AOI, the requested northern edge came back ~0.023 deg
        # short, so a poleward strip can read as nodata, while ~0.5 deg of
        # unrequested ground is added on each side. Small AOIs (the common case)
        # are affected at the ~0.002 deg level. Fixing it properly needs the
        # densified back-transform upstream; see the roadmap's PE follow-up.
        return transformer.transform_bounds(*latlon_bbox)

    def _eedai_output_grid(
        self, bbox: tuple[float, float, float, float], scale: float
    ) -> tuple[int, int]:
        """Size a pixel grid for `bbox` (already in the output CRS) at `scale`.

        Dispatches on the output CRS: EPSG:4326 keeps the geographic grid with
        its `cos(latitude)` longitude shortening (:meth:`_eedai_grid`); a
        metre-based projected CRS sizes each axis by its span over the metre
        `scale` directly.

        Args:
            bbox: The window `(min_x, min_y, max_x, max_y)` in `self.crs`.
            scale: Target ground sample distance in metres.

        Returns:
            `(rows, cols)`, at least one pixel per axis.
        """
        if self.crs.upper() == _EEDAI_NATIVE_CRS:
            return self._eedai_grid(bbox, scale)
        if not all(math.isfinite(bound) for bound in bbox):
            # `transform_bounds` returns infinities for an AOI outside the
            # projection's domain; without this the budget maths would raise an
            # opaque OverflowError from `math.ceil` instead.
            raise ValueError(
                f"the AOI bounds must all be finite, got {bbox!r} in {self.crs}"
            )
        if not scale or scale <= 0:
            raise ValueError("'scale' must be a positive number of metres.")
        min_x, min_y, max_x, max_y = (float(v) for v in bbox)
        rows = max(math.ceil((max_y - min_y) / scale), 1)
        cols = max(math.ceil((max_x - min_x) / scale), 1)
        return rows, cols

    @staticmethod
    def _reader_end(bucket_end: dt.datetime) -> str:
        """Convert an exclusive bucket end to the inclusive date the reader wants.

        This backend's buckets are half-open, matching Earth Engine's
        `filterDate`, whose `end` is exclusive. The reader's `end` is
        *inclusive*: a bare date becomes `startTime < <end + 1 day>`. Passing
        the exclusive boundary straight through would therefore read one extra
        day per bucket and make consecutive buckets overlap.

        Args:
            bucket_end: The bucket's exclusive end.

        Returns:
            The `YYYY-MM-DD` date the reader should treat as inclusive, so its
            window covers exactly the same instants as `filterDate`.
        """
        return (bucket_end - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    def _eedai_latlon_aoi(self) -> tuple[float, float, float, float]:
        """Return the AOI envelope in EPSG:4326 for EEDA scene discovery.

        Scene discovery queries the Earth Engine catalog in lat/lon, so the AOI
        is given in EPSG:4326 whatever the output CRS.

        It must describe the *same ground the read will window*, or the scene
        count gating the read would be discovered over one geometry while the
        pixel footprint is computed over another. A `region` supersedes the
        lat/lon bbox for the clip, so its bounds are the AOI here too, brought
        back to lat/lon when the region carries another CRS.

        Returns:
            `(min_lon, min_lat, max_lon, max_lat)`.
        """
        region = self.region
        if region is not None:
            latlon = region
            crs = getattr(region, "crs", None)
            if crs is not None and not self._same_crs(crs, _EEDAI_NATIVE_CRS):
                latlon = region.to_crs(_EEDAI_NATIVE_CRS)
            min_x, min_y, max_x, max_y = (float(v) for v in latlon.total_bounds)
            return (min_x, min_y, max_x, max_y)
        return (
            self.space.longitude_min,
            self.space.latitude_min,
            self.space.longitude_max,
            self.space.latitude_max,
        )

    def _eedai_collection_fits(
        self,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None,
        bucket_end: dt.datetime | None,
    ) -> EedaiPlan:
        """Decide whether the reader can composite this collection bucket.

        The reader downloads every scene the bucket's date window and AOI
        select and holds them in memory to reduce, so the cost is the scene
        count times the AOI's native footprint. Both come from EEDA's own
        per-scene fields via `estimate_earthengine_cost` — a fact about the
        scenes, not a guess from asset metadata. A bucket with more scenes
        than :data:`_EEDAI_MAX_SCENES`, or whose scenes together exceed the
        single-pass pixel budget, is declined so Earth Engine's server-side
        reduce serves it instead.

        Args:
            var_info: The collection's catalog entry.
            band_count: Bands requested; the reader holds every band per scene.
            bucket_start: Inclusive start of the bucket's date window.
            bucket_end: Exclusive end of the bucket's date window.

        Returns:
            An :class:`EedaiPlan`; a collection is served in one pass
            (`tile_size` is `None`) or declined with a reason.
        """
        if bucket_start is None or bucket_end is None:
            # An internal contract violation, not a property of the request: a
            # decline here would hide the caller's mistake as a permanent,
            # silent fallback to Earth Engine.
            raise ValueError(
                f"a collection read of {var_info.id} needs a bucket date window; "
                "_api passes one for every bucket"
            )
        reducer = self.reducer or var_info.default_reducer
        if reducer in _EEDAI_UNSUPPORTED_REDUCERS:
            # Checked before the network call: no point discovering scenes for a
            # composite the reader cannot reproduce.
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"reducer={reducer!r} does not mean the same thing client-side: "
                    "Earth Engine's is last-wins, while the reader returns the first "
                    "scene of the stack, so Earth Engine composites this instead"
                ),
            )
        reader = import_earthengine_reader()
        # Built outside the discovery `try`, so a credential problem is never
        # reported as "no scenes". Under a forced engine it raises; under
        # `auto` it warns and falls back, because `auto`'s contract is to route
        # a request Earth Engine can still serve rather than to fail it - but
        # loudly, so a fixable key does not silently disable the fast path.
        try:
            credentials = self._eedai_credentials()
        except AuthenticationError:
            if self.engine == "eedai":
                raise
            logger.warning(
                f"the EEDAI credential could not be built, so {var_info.id} "
                "falls back to Earth Engine; fix the service key to use the "
                "fast path."
            )
            return EedaiPlan(False, None, 0, "the EEDAI credential could not be built")
        window_start = bucket_start.strftime("%Y-%m-%d")
        window_end = self._reader_end(bucket_end)
        # One catalog query per bucket, by necessity: every bucket has a
        # distinct window, and `ReadCost` reports only aggregates, so a single
        # whole-run discovery cannot be split back into per-bucket counts.
        # Caching keyed on the window was measured to never hit for this reason
        # and was removed rather than left in as dead weight. Reducing this to
        # one query per run needs upstream to expose the per-scene times.
        try:
            cost = reader.estimate_earthengine_cost(
                var_info.id,
                start=window_start,
                end=window_end,
                bbox=self._eedai_latlon_aoi(),
                # The AOI handed over is lat/lon, so it must be labelled as
                # such: upstream reads `bbox` *in* `crs`, and passing the
                # output CRS here would have degrees read as projected metres
                # and discover scenes over the wrong ground.
                crs=_EEDAI_NATIVE_CRS,
                credentials=credentials,
                property_filter=self.property_filter,
            )
        except _reader_errors(reader) as exc:
            # A discovery failure is a fallback, not a crash - but it is
            # worth more than an info line, because a persistent one
            # silently disables the fast path for the whole run.
            logger.warning(
                f"EEDA scene discovery failed for {var_info.id}: {exc}. This "
                "bucket falls back to Earth Engine."
            )
            return EedaiPlan(
                False, None, 0, f"scene discovery for {var_info.id} failed ({exc})"
            )
        if not cost.scene_count:
            # A legitimate, quiet decline: the window and AOI simply hold no
            # scenes, so there is nothing for the reader to composite.
            return EedaiPlan(
                False,
                None,
                0,
                f"no {var_info.id} scenes in this bucket's window and AOI",
            )
        if cost.scene_count > _EEDAI_MAX_SCENES:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{cost.scene_count:,} scenes in this bucket, over the "
                    f"{_EEDAI_MAX_SCENES:,}-scene cap — Earth Engine reduces this "
                    "server-side instead of the reader fetching every scene"
                ),
            )
        # `estimate_earthengine_cost` is authoritative for the *scene count*;
        # the per-scene native footprint is sized from the catalog's metre
        # `spatial_resolution`, since EEDA reports `min_pixel_size` in the
        # asset's own CRS units (degrees for a geographic asset), which the
        # metre-based grid would misread by ~1e5x.
        native_scale = var_info.spatial_resolution
        if not native_scale:
            return EedaiPlan(
                False,
                None,
                0,
                f"{var_info.id} has no native resolution to size the read",
            )
        bbox, _cutline = self._eedai_window()
        # One scene's window must satisfy the same budgets a single-image read
        # does - the per-axis cap is about the window's *shape*, which no
        # scene-count multiple would catch - so the shared gate runs first.
        fits, reason = self._eedai_native_fits(var_info, bbox, band_count)
        if not fits:
            return EedaiPlan(False, None, 0, reason)
        # Every scene is warped onto the *output* window and the whole set is
        # held to reduce, so the stack is sized by the output grid - not the
        # native one. A `scale` finer than the asset makes the output grid the
        # larger of the two, which is exactly when sizing from native
        # under-counts what has to fit in memory.
        native_rows, native_cols = self._eedai_output_grid(bbox, float(native_scale))
        out_rows, out_cols = self._eedai_output_grid(
            bbox, float(self.scale or native_scale)
        )
        rows = max(native_rows, out_rows)
        cols = max(native_cols, out_cols)
        total = cost.scene_count * rows * cols * max(band_count, 1)
        if total > _EEDAI_MAX_PIXELS:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"about {total:,} px across {cost.scene_count:,} scenes on a "
                    f"{cols}x{rows} grid, over the {_EEDAI_MAX_PIXELS:,}-px "
                    "single-pass budget"
                ),
            )
        return EedaiPlan(True, None, cost.scene_count, "")

    def _eedai_verdict(
        self,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None,
        bucket_end: dt.datetime | None,
    ) -> EedaiPlan:
        """Build the serve/decline verdict, dispatching on the asset kind.

        A single image is sized by :meth:`_eedai_single_image_plan`; an image collection is
        sized by :meth:`_eedai_collection_fits`, which counts the scenes the
        reader would fetch and reduce.

        Args:
            var_info: The catalog entry.
            band_count: Bands requested.
            bucket_start: Inclusive start of the bucket window (collections).
            bucket_end: Exclusive end of the bucket window (collections).

        Returns:
            The :class:`EedaiPlan` verdict.
        """
        if var_info.is_image_collection:
            return self._eedai_collection_fits(
                var_info, band_count, bucket_start, bucket_end
            )
        return self._eedai_single_image_plan(var_info, band_count)

    def _eedai_single_image_plan(self, var_info: Dataset, band_count: int) -> EedaiPlan:
        """Decide how — or whether — the reader can serve this request.

        A window too large to materialise is no longer a dead end: the reader
        can stream it to disk one tile at a time and mosaic the result, which
        is what retires `auto_split` for this path.

        Tiling is declined — and the request falls back to Earth Engine — in
        five cases:

        * the asset has no catalogued native resolution, so a per-tile read
          cannot be sized;
        * the request is much coarser than the asset (`native_ratio` above
          :data:`_EEDAI_MAX_TILING_RATIO`), where Earth Engine's server-side
          aggregation returns a small raster instead of fetching `ratio**2`
          native pixels per output pixel;
        * the whole read would still fetch more than
          :data:`_EEDAI_MAX_NATIVE_PIXELS` — the tile budget bounds memory,
          this bounds the work;
        * `resample` is not `"nearest"`, which upstream refuses because an
          interpolating kernel would disagree at the tile seams;
        * a polygon cutline is set, which upstream also refuses.

        A sixth case declines late: if the reader's block padding consumes the
        whole per-tile allowance there is no workable tile to cut, so the read
        falls back rather than dividing by a zero-sized tile.

        Args:
            var_info: The catalog entry being fetched.
            band_count: How many bands the read asks for; the reader holds
                them all, so they divide the per-tile budget.

        Returns:
            An :class:`EedaiPlan`. `tile_size` is `None` for a single read, and
            `reason` explains a `False` for the fallback log line or the
            forced-engine error.
        """
        bbox, cutline = self._eedai_window()
        fits, reason = self._eedai_native_fits(var_info, bbox, band_count)
        if fits:
            return EedaiPlan(True, None, 1, "")
        native_scale = var_info.spatial_resolution
        if not native_scale:
            return EedaiPlan(False, None, 0, reason)
        if cutline is not None:
            return EedaiPlan(
                False, None, 0, f"{reason}, and it cannot be tiled behind a cutline"
            )
        if self.resample != _EEDAI_TILING_RESAMPLE:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{reason}, and it cannot be tiled with resample="
                    f"{self.resample!r} — an interpolating resampler would differ "
                    "from the un-tiled read at the tile seams"
                ),
            )
        scale_m = float(self.scale or native_scale)
        native_ratio = max(scale_m / float(native_scale), 1.0)
        if native_ratio > _EEDAI_MAX_TILING_RATIO:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{reason}, and tiling it would be worse than Earth Engine: at "
                    f"scale={scale_m:g} m over a {native_scale:g} m asset the reader "
                    f"fetches about {native_ratio**2:,.0f} native px per output px, "
                    "which Earth Engine aggregates server-side instead"
                ),
            )
        native_rows, native_cols = self._eedai_output_grid(bbox, float(native_scale))
        native_total = native_rows * native_cols * max(band_count, 1)
        if native_total > _EEDAI_MAX_NATIVE_PIXELS:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{reason}, and tiling it would still fetch about "
                    f"{native_total:,} native px, over the "
                    f"{_EEDAI_MAX_NATIVE_PIXELS:,}-px ceiling on one read's total work"
                ),
            )
        # One tile's native read is `tile_size * scale / native_scale` px per
        # side and is held in memory whole, so shrink the tile until that read
        # satisfies *both* budgets the single-pass gate applies — the per-axis
        # cap and the total-pixel one.
        # Budgets are on the *native* footprint, which is the nominal window
        # plus the reader's block alignment and pad, so the allowance is
        # spent before dividing back into output pixels.
        axis_allowance = EE_MAX_DIMENSION - _EEDAI_WINDOW_PAD
        area_allowance = (
            math.sqrt(_EEDAI_MAX_PIXELS / max(band_count, 1)) - _EEDAI_WINDOW_PAD
        )
        # Not floored: an allowance the padding has already exhausted must
        # reach the guard below, not be rounded up into a one-pixel tile.
        tile_size = int(
            min(
                _EEDAI_TILE_PIXELS,
                axis_allowance / native_ratio,
                area_allowance / native_ratio,
            )
        )
        if tile_size < 1:
            # Defensive: with the shipped constants the ratio bound leaves a
            # workable tile, but this guard is deliberately kept — it was
            # removed once as unreachable, and the next change to the padding
            # made the plan divide by a zero-sized tile.
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{reason}, and no tile is small enough: the reader's "
                    f"{_EEDAI_WINDOW_PAD}-px window padding already exceeds the "
                    "per-tile budget here"
                ),
            )
        rows, cols = self._eedai_output_grid(bbox, scale_m)
        tiles = math.ceil(rows / tile_size) * math.ceil(cols / tile_size)
        if tiles > _EEDAI_MAX_TILES:
            return EedaiPlan(
                False,
                None,
                0,
                (
                    f"{reason}, and tiling it would take {tiles:,} tiles (over the "
                    f"{_EEDAI_MAX_TILES:,}-tile ceiling); every tile is its own "
                    "fetch and they are opened together to mosaic"
                ),
            )
        return EedaiPlan(True, tile_size, tiles, "")

    def _use_eedai(
        self,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> tuple[bool, EedaiPlan | None]:
        """Resolve the configured `engine` against this request's eligibility.

        Args:
            var_info: The catalog entry for the dataset being fetched.
            band_count: How many bands the read asks for; the reader holds
                them all, so they divide the per-tile budget.
            bucket_start: Inclusive start of the bucket's date window, needed
                to size an `image_collection` read (the reader composites the
                scenes it selects). `None` for a single-image read.
            bucket_end: Exclusive end of that window.

        Returns:
            `(use_reader, plan)`. The plan is built here — after the
            short-circuits, so a request that opted out of this engine never
            pays for its sizing nor inherits its failure modes — and handed
            back so the read that follows is the same decision rather than a
            second one. It is `None` whenever `use_reader` is `False`.
            Under `"auto"` a request the plan declines falls back rather than
            failing: the user asked for a download, not for this engine.

        Raises:
            ValueError: If `engine="eedai"` was forced and the request is
                either ineligible — it needs server-side compute (a reduced
                collection, a `cloud_mask` or `filters`) or targets a CRS the
                reader cannot size (not EPSG:4326 or a metre-based projected
                CRS) — or eligible but declined by :meth:`_eedai_verdict`,
                which the message names: the asset has no native resolution,
                the window is behind a polygon cutline, `resample` is not
                nearest-neighbour, or tiling it would cost more than Earth
                Engine would (too coarse a `scale` over a fine asset, too many
                native pixels, or too many tiles).
        """
        if self.engine == "ee":
            return False, None
        eligible = self._eedai_eligible(var_info)
        if self.engine == "eedai":
            if not eligible:
                raise ValueError(
                    f"engine='eedai' cannot serve {var_info.id}: the EEDAI "
                    "reader materialises pixels from an asset id, so it cannot "
                    "run server-side compute (a reduced collection, cloud_mask "
                    "or filters), and it reads only EPSG:4326 or a metre-based "
                    f"projected CRS (got crs={self.crs!r}). Use engine='auto' "
                    "or engine='ee'."
                )
            plan = self._eedai_verdict(var_info, band_count, bucket_start, bucket_end)
            if not plan.can_serve:
                raise ValueError(
                    f"engine='eedai' cannot serve {var_info.id}: {plan.reason}. Use a "
                    "smaller bbox, engine='ee' (with auto_split=True to tile), or "
                    "export_via='drive'."
                )
            return True, plan
        if not (eligible and eedai_available()):
            return False, None
        plan = self._eedai_verdict(var_info, band_count, bucket_start, bucket_end)
        if not plan.can_serve:
            logger.info(
                f"Serving {var_info.id} through Earth Engine rather than the EEDAI "
                f"reader: {plan.reason}."
            )
            return False, None
        return True, plan

    def _eedai_window(self) -> tuple[tuple[float, float, float, float], Any]:
        """Return the AOI the reader should read, as `(bbox, cutline)`.

        The reader takes `bbox` as the read window and only falls back to a
        `geometry`'s envelope when no `bbox` is given, so both are returned
        together: the bbox always describes the window the pixel grid is
        sized for, and the cutline (when a `region` was passed) clips the
        result to the exact polygon. Deriving the bbox from the region's own
        bounds is what keeps the window and the grid in agreement — sizing a
        bbox-shaped grid for a region-shaped window would silently change the
        ground resolution by the ratio of the two extents.

        Returns:
            `(bbox, cutline)` — the `(min_x, min_y, max_x, max_y)` window in
            the output CRS (lat/lon under EPSG:4326, the projection's metres
            otherwise), and the `region` to clip to or `None`.
        """
        region = self._region_in_output_crs(self.region)
        if region is not None:
            min_x, min_y, max_x, max_y = (float(v) for v in region.total_bounds)
            return (min_x, min_y, max_x, max_y), region
        latlon = (
            self.space.longitude_min,
            self.space.latitude_min,
            self.space.longitude_max,
            self.space.latitude_max,
        )
        return self._bbox_to_output_crs(latlon), None

    def _region_in_output_crs(self, region: Any) -> Any:
        """Return `region` in the output CRS the reader's `bbox` is read in.

        The bbox and the cutline must share one space: the reader sizes the
        pixel grid from the bbox and clips to the cutline, so a region left in
        a different CRS would window one patch of ground and clip another.
        Reprojecting the region into `self.crs` once keeps them aligned.

        Args:
            region: The constructor `region`, or `None`.

        Returns:
            The region in `self.crs` (`None` passes through). A region with no
            CRS is assumed to be lat/lon, matching how the Earth Engine path
            treats it, and is reprojected only when the output CRS is not
            EPSG:4326.
        """
        if region is None:
            return None
        target = self.crs
        crs = getattr(region, "crs", None)
        if crs is None:
            if target.upper() == _EEDAI_NATIVE_CRS:
                return region
            set_crs = getattr(region, "set_crs", None)
            if callable(set_crs):
                region = set_crs(_EEDAI_NATIVE_CRS)
            return region.to_crs(target)
        if self._same_crs(crs, target):
            return region
        return region.to_crs(target)

    @staticmethod
    def _same_crs(region_crs: Any, target: str) -> bool:
        """Return whether a region's CRS already is the output CRS.

        Compared as CRS objects rather than by string-parsing an `AUTH:CODE`
        tail: discarding the authority makes a non-EPSG code collide with the
        EPSG code of the same number (an `ESRI:3857` target would accept an
        EPSG:3857 region unreprojected), and a PROJ string or WKT target has no
        code to parse at all, so an already-correct region would be warped
        needlessly.

        Args:
            region_crs: The region's own CRS object.
            target: The output CRS, in any form pyproj accepts.

        Returns:
            `True` when the two describe the same CRS; `False` when they differ
            or either cannot be parsed (in which case reprojecting is the safe
            answer).
        """
        from pyproj import CRS

        try:
            return bool(CRS.from_user_input(region_crs) == CRS.from_user_input(target))
        except Exception:  # noqa: BLE001 - fall back below rather than assume a match
            pass
        # A CRS object pyproj cannot parse may still report an EPSG code. Trust
        # that only against an explicitly EPSG target, so the authority is still
        # part of the comparison rather than dropped.
        to_epsg = getattr(region_crs, "to_epsg", None)
        if not callable(to_epsg) or not target.upper().startswith("EPSG:"):
            return False
        code = target.split(":", 1)[1]
        return bool(code.isdigit() and to_epsg() == int(code))

    @staticmethod
    def _eedai_grid(
        bbox: tuple[float, float, float, float], scale: float
    ) -> tuple[int, int]:
        """Size a pixel grid for a lat/lon `bbox` at a metre `scale`.

        The reader sizes its output in the units of the output CRS (degrees
        here), so the metre `scale` has to become an explicit grid. This is
        deliberately not :meth:`SpatialExtent.estimate_pixel_dims`, which
        pyramids documents as a worst-case *upper bound* for cap pre-checks
        — it over-counts both axes (and unevenly, so a square AOI comes out
        non-square). Here the real span is used, with longitude degrees
        shortened by `cos(latitude)` at the AOI's mid-latitude, so the pixels
        are square on the ground at the requested `scale`.

        Args:
            bbox: The lat/lon window `(min_x, min_y, max_x, max_y)`.
            scale: Target ground sample distance in metres.

        Returns:
            `(rows, cols)` — at least one pixel per axis, so a sub-pixel AOI
            still yields a readable raster rather than a zero-sized one, and
            never coarser on the ground than the requested `scale`.

        Raises:
            ValueError: If any bound is not finite, or `scale` is not a
                positive number of metres.

        Examples:
            - A 0.1° box over Cairo at 90 m is taller than it is wide in
              pixels: a degree of longitude is shorter at that latitude, and
              the grid is sized at the box's poleward edge:
                ```python
                >>> from earthlens.gee.backend import GEE
                >>> GEE._eedai_grid((31.2, 29.9, 31.3, 30.0), 90.0)
                (124, 108)

                ```
            - Spanning the equator the two axes match, because the poleward
              edge is 1° and a degree of longitude is barely shortened there:
                ```python
                >>> from earthlens.gee.backend import GEE
                >>> GEE._eedai_grid((0.0, 0.0, 1.0, 1.0), 1000.0)
                (112, 112)

                ```
            - An AOI smaller than one pixel still yields a readable raster:
                ```python
                >>> from earthlens.gee.backend import GEE
                >>> GEE._eedai_grid((31.2, 29.9, 31.2001, 29.9001), 90.0)
                (1, 1)

                ```
        """
        min_x, min_y, max_x, max_y = bbox
        if not all(math.isfinite(bound) for bound in bbox):
            raise ValueError(f"the AOI bounds must be finite, got {bbox}")
        if scale <= 0 or not math.isfinite(scale):
            raise ValueError(f"scale must be a positive number of metres, got {scale}")
        # Take `cos` at the poleward edge rather than the mid-latitude: for a
        # tall AOI the mid-latitude value would under-count columns nearer the
        # pole, sampling coarser than asked. The poleward edge only ever errs
        # finer. Clamped away from the pole itself, where `cos` reaches zero.
        poleward = min(max(abs(min_y), abs(max_y)), 89.9)
        height_m = abs(max_y - min_y) * _METRES_PER_DEGREE
        width_m = (
            abs(max_x - min_x) * _METRES_PER_DEGREE * math.cos(math.radians(poleward))
        )
        # Round up, not to nearest: rounding down would leave the raster a
        # little coarser than the scale that was asked for.
        rows = max(1, math.ceil(height_m / scale))
        cols = max(1, math.ceil(width_m / scale))
        return rows, cols

    def _warn_cog_ignored(self, var_info: Dataset) -> None:
        """Say so, once, when `cog=True` cannot apply to this request.

        `cog=` only reaches the EEDAI writer, so a request that stays on
        Earth Engine silently yields a plain GeoTIFF. Without a notice the
        only symptom is an output that is not a COG.

        Args:
            var_info: The catalog entry being written (named in the notice).
        """
        if not self.cog or self._cog_warned:
            return
        self._cog_warned = True
        logger.warning(
            f"cog=True has no effect for {var_info.id}: it applies to the EEDAI "
            "path, and this request is served by Earth Engine (see engine=). A "
            "plain GeoTIFF is written instead."
        )

    def _warn_property_filter_ignored(self, var_info: Dataset) -> None:
        """Say so, once, when `property_filter=` cannot apply to this request.

        `property_filter` narrows scenes only on the EEDAI *collection* path.
        This is called on the Earth Engine fallback branch, so reaching it means
        the filter is being dropped: a single image, an Earth Engine-served
        request, or - the case that matters most - an eligible collection whose
        bucket declined (over budget, a discovery failure, an unsupported
        reducer). Without the notice the user silently gets a composite built
        from every scene, and a multi-bucket run can mix filtered and unfiltered
        buckets in one series.

        Args:
            var_info: The catalog entry being written (named in the notice).
        """
        if self.property_filter is None or var_info.id in self._property_filter_warned:
            return
        # Per dataset, not per run: a multi-dataset request can serve one
        # collection through the reader and drop the filter on another, and a
        # single global notice would name only the first.
        self._property_filter_warned.add(var_info.id)
        logger.warning(
            f"property_filter has no effect for {var_info.id}: this request is "
            "served by Earth Engine, which cannot apply it, so the composite is "
            "built from every scene in the window - cloudy ones included. It "
            "narrows scenes only on the EEDAI collection path (see engine=)."
        )

    def _eedai_credentials(self) -> Any:
        """Return the pyramids-eo credential for EEDAI reads, built once.

        `EarthEngineCredentials` writes inline key material to a private
        temp file whose removal is left to the garbage collector, so
        rebuilding it per bucket would scatter transient key files across a
        multi-band, multi-date download. It is therefore resolved once per
        instance and reused.

        The Earth Engine `project` is deliberately not forwarded: the reader
        authenticates GDAL's `EEDAI:` driver with the key alone. When no key
        resolves at all the reader falls back to Application Default
        Credentials, which may be a *different* identity from the one the
        Earth Engine half uses, so that case is logged rather than silent.

        Returns:
            The `pyramids_eo.earthengine.EarthEngineCredentials` to read with.

        Raises:
            AuthenticationError: If the credential cannot be built, so the
                failure matches this backend's error contract rather than
                surfacing pyramids-eo's own exception type.
            ImportError: If `pyramids-eo` (the `[eedai]` extra) is missing.
        """
        if self._eedai_credential is not None:
            return self._eedai_credential
        _service_account, service_key, _project = self._resolve_credentials()
        if service_key is None:
            logger.warning(
                "No Earth Engine service key resolved for the EEDAI read; falling "
                "back to Application Default Credentials, which may authenticate "
                "as a different identity than the Earth Engine half of this "
                "request. Pass service_key= (or set GEE_SERVICE_KEY) to pin it."
            )
        try:
            self._eedai_credential = credentials_for(service_key)
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"could not build Earth Engine credentials for the EEDAI read: {exc}"
            ) from exc
        return self._eedai_credential

    def _eedai_native_fits(
        self,
        var_info: Dataset,
        bbox: tuple[float, float, float, float],
        band_count: int,
    ) -> tuple[bool, str]:
        """Report whether the reader's native-resolution read is bounded.

        The EEDAI driver's overviews are unreliable, so the reader fetches
        the AOI at the asset's *native* resolution and materialises it in
        memory before downsampling — a wide AOI over a fine-resolution asset
        is a huge read however coarse the requested `scale`.

        An asset with no catalogued `spatial_resolution` counts as not
        fitting, rather than as safe: an unknown native grid is exactly the
        case that cannot be sized up front.

        This answers only "can one pass hold it?". A window that does not fit
        is not necessarily refused — :meth:`_eedai_single_image_plan` may still serve it by
        streaming in tiles — so this reports rather than raises.

        Args:
            var_info: The catalog entry (for the asset's native resolution).
            bbox: The output-CRS window the reader would materialise.
            band_count: How many bands the read asks for. The reader holds
                every requested band of the window at once, so the budget is
                spent per band.

        Returns:
            `(fits, reason)` — `reason` is empty when it fits, and otherwise
            explains why in a form suitable for a log line or an error.
        """
        native_scale = var_info.spatial_resolution
        if not native_scale:
            return False, (
                f"{var_info.id} has no catalogued native resolution, so the "
                "reader's native-resolution read cannot be bounded up front"
            )
        # The warp holds whichever grid is larger: a `scale` finer than the
        # asset makes the output bigger than the native window. Fold them
        # together before *either* budget is applied.
        native_rows, native_cols = self._eedai_output_grid(bbox, float(native_scale))
        out_rows, out_cols = self._eedai_output_grid(
            bbox, float(self.scale or native_scale)
        )
        rows = max(native_rows, out_rows)
        cols = max(native_cols, out_cols)
        binding = (
            "native"
            if (rows, cols) == (native_rows, native_cols)
            else f"{self.scale or native_scale} m output"
        )
        if max(rows, cols) > EE_MAX_DIMENSION:
            return False, (
                f"the AOI is about {cols}x{rows} px on {var_info.id}'s {binding} "
                f"grid, over the {EE_MAX_DIMENSION}-px per-axis budget the reader "
                "would hold in memory"
            )
        bands_held = max(band_count, 1)
        total_px = rows * cols * bands_held
        if total_px > _EEDAI_MAX_PIXELS:
            return False, (
                f"the AOI is about {cols * rows:,} px across {bands_held} band(s) "
                f"= {total_px:,} px on {var_info.id}'s {binding} grid, over the "
                f"{_EEDAI_MAX_PIXELS:,}-px budget the reader would hold in memory"
            )
        return True, ""

    def _export_via_eedai(
        self,
        var_info: Dataset,
        bands: list[str],
        scale: float,
        prefix: str,
        plan: EedaiPlan,
        bucket_start: dt.datetime | None = None,
        bucket_end: dt.datetime | None = None,
    ) -> Path:
        """Materialise one raw asset through the pyramids-eo EEDAI reader.

        Reads the requested bands straight from the asset via GDAL's `EEDAI`
        driver into a pyramids `Dataset` — reprojected to `crs`, clipped to
        the AOI — and writes it to `<prefix>.tif`. There is no
        `getDownloadURL` round-trip, so Earth Engine's 32768-px synchronous
        cap (and `auto_split`) does not apply.

        The reader sizes its output in the units of `crs` (degrees, since
        this path is EPSG:4326-only), whereas `scale` here is Earth Engine's
        metres. :meth:`_eedai_grid` reconciles the two by turning `scale`
        into an explicit `shape` over the same window
        :meth:`_eedai_window` hands the reader, so the grid and the read
        window always describe the same ground area.

        The raster is written as a plain GeoTIFF, or as a Cloud Optimized
        GeoTIFF (tiled, with overviews) when `cog=True` was passed to the
        constructor.

        Args:
            var_info: The catalog entry; its `id` is the Earth Engine asset.
            bands: Band ids to read.
            scale: Output pixel size in metres.
            prefix: Output filename stem (no extension).
            plan: The :class:`EedaiPlan` verdict from :meth:`_eedai_verdict`,
                computed once by the caller so the routing decision and the
                read it performs cannot disagree.
            bucket_start: Inclusive start of the bucket's date window. Required
                for an `image_collection`, whose scenes the reader composites;
                ignored for a single image.
            bucket_end: Exclusive end of that window.

        Returns:
            The :class:`pathlib.Path` of the written GeoTIFF.

        Raises:
            ImportError: If `pyramids-eo` (the `[eedai]` extra) is missing.
            AuthenticationError: If the reader's credentials cannot be built.
        """
        reader = import_earthengine_reader()
        credentials = self._eedai_credentials()
        target = self.root_dir / f"{prefix}.tif"
        # Write beside the target and rename on success: `to_file` / `to_cog`
        # write in place, so a mid-write failure would otherwise leave a
        # truncated raster sitting at the final name for a later run to read
        # as a finished product.
        staged = self.root_dir / f"{prefix}.partial.tif"
        # A tiled read has already written `staged`, so a COG conversion needs a
        # second name rather than using its source as its own destination.
        cog_staged = self.root_dir / f"{prefix}.partial-cog.tif"
        bbox, cutline = self._eedai_window()
        if not plan.can_serve:
            # Only reachable if a caller bypasses `_use_eedai`; taking the read
            # anyway would be the unguarded path the plan exists to prevent.
            raise ValueError(
                f"the EEDAI reader cannot serve {var_info.id}: {plan.reason}"
            )
        composite_kwargs: dict[str, Any] = {}
        if var_info.is_image_collection:
            if bucket_start is None or bucket_end is None:
                raise ValueError(
                    f"a collection read of {var_info.id} needs a bucket window"
                )
            # The reader composites the scenes in this window with the same
            # reducer the Earth Engine path would use.
            #
            # Caveat: the reader is given no `nodata`, because neither the EEDAI
            # driver nor this catalog declares one, so its statistical reducers
            # run unmasked and fold a scene's fill pixels into the result where
            # Earth Engine would mask them. The values agree wherever the scenes
            # carry no fill over the AOI. Supplying a per-band fill (upstream
            # pyramids-eo#63) is what would close the gap.
            composite_kwargs = {
                "start": bucket_start.strftime("%Y-%m-%d"),
                "end": self._reader_end(bucket_end),
                "reducer": self.reducer or var_info.default_reducer,
            }
            if self.property_filter is not None:
                composite_kwargs["property_filter"] = self.property_filter
        read_options: dict[str, Any] = {}
        tile_size = plan.tile_size
        if tile_size is not None:
            # Too large for one pass: have the reader stream the mosaic to disk
            # a tile at a time rather than hold the whole window in memory.
            read_options = {"tile_size": tile_size, "path": str(staged)}
            logger.info(
                f"Streaming {var_info.id} through the EEDAI reader as {plan.tiles:,} "
                f"tile(s) of {tile_size} px."
            )
        # The tiled read writes `staged` itself, so it belongs inside the same
        # `try` as the write: a mosaic that fails partway would otherwise leave
        # its partial file behind.
        try:
            dataset = reader.from_earthengine(
                var_info.id,
                bands=list(bands),
                window=reader.Window(
                    bbox=bbox,
                    crs=self.crs,
                    shape=self._eedai_output_grid(bbox, scale),
                    resample=self.resample,
                ),
                geometry=cutline,
                credentials=credentials,
                **composite_kwargs,
                **read_options,
            )
            try:
                if self.cog:
                    dataset.cog.to_cog(str(cog_staged))
                elif tile_size is None:
                    dataset.to_file(str(staged))
            finally:
                close_quietly(dataset)
                # Drop the last reference before the rename: closing alone
                # leaves the GDAL object alive in this frame, so a collect
                # inside the retry would have nothing to free.
                dataset = None
            _rename_when_unlocked(cog_staged if self.cog else staged, target)
        finally:
            _discard_quietly(staged)
            _discard_quietly(cog_staged)
        logger.info(f"Wrote {target} (EEDAI{', COG' if self.cog else ''})")
        return target

    def _client(self) -> HttpClient:
        """Return this instance's HTTP client, built once.

        A tiled export issues one download per tile against the same host, so
        the client (and its pooled connection) is held on the instance rather
        than rebuilt per tile. The import stays local, as elsewhere in this
        module, so importing the backend does not pull the HTTP stack.

        Returns:
            HttpClient: The shared client.
        """
        from earthlens.base.http import HttpClient

        if self._http is None:
            self._http = HttpClient(timeout=self.http_timeout)
        return self._http

    def _download_one_url_tile(self, image, region, scale: float, prefix: str) -> Path:
        """Issue one `getDownloadURL` request → tif at `<prefix>.tif`.

        Single-tile worker shared by the small-AOI path and the
        auto-split loop. Stripped of size-checking — callers are
        expected to have already verified that the request fits the
        Earth Engine synchronous limit.
        """
        url = image.getDownloadURL(
            {"scale": scale, "crs": self.crs, "region": region, "format": "GEO_TIFF"}
        )
        target = self.root_dir / f"{prefix}.tif"
        # Route the (single-shot, expiring) getDownloadURL fetch through the
        # shared HttpClient so a transient 429/5xx is retried with back-off
        # instead of failing the tile outright.
        client = self._client()
        # Stream to a temp rather than buffering `response.content`: a tile is
        # capped at 32768 px/axis, so a single band can run to hundreds of
        # megabytes and the old path held it whole just to inspect four bytes.
        # GEE returns either a bare GeoTIFF or a zip of them, so the format is
        # decided from the leading bytes on disk.
        staged = self.root_dir / f"{prefix}.download"
        try:
            client.download(url, staged, progress=False)
            with open(staged, "rb") as handle:
                is_zip = handle.read(4) == _ZIP_MAGIC
            size = staged.stat().st_size
            if is_zip:
                PyramidsDataset.from_archive(
                    staged,
                    kind="zip",
                    member_glob="*.tif",
                    path=str(target),
                )
            else:
                # Release the reader before the `finally` unlink: pyramids keeps
                # the GDAL handle open, which holds a Windows lock on `staged`
                # (the same reason ghsl closes before its rename).
                reader = PyramidsDataset.read_file(str(staged))
                try:
                    reader.to_file(str(target))
                finally:
                    close_quietly(reader)
        finally:
            staged.unlink(missing_ok=True)
        logger.info(f"Wrote {target} ({size} bytes)")
        return target

    def _auto_split_and_download(
        self, image, var_info: Dataset, scale: float, prefix: str
    ) -> Path:
        """Tile an oversized AOI, download each tile, mosaic into one GeoTIFF.

        Only reachable when `auto_split=True` was passed to the
        constructor and the full AOI exceeds :data:`EE_MAX_DIMENSION` px
        per axis. The bbox is split with :func:`split_aoi_for_url`, each
        sub-extent is downloaded via :meth:`_download_one_url_tile`, and
        the per-tile tifs are mosaicked into `<prefix>.tif` with
        :func:`pyramids.dataset.merge.merge_rasters`. Per-tile tifs are
        deleted on success.
        """
        sub_extents = split_aoi_for_url(self.space, scale)
        logger.info(
            f"{var_info.id}: AOI exceeds {EE_MAX_DIMENSION}-px per-axis cap at "
            f"scale={scale} m; auto-splitting into {len(sub_extents)} tile(s)."
        )
        tile_paths: list[Path] = []
        for k, sub in enumerate(sub_extents):
            sub_region = ee.Geometry.Rectangle(
                [sub.west, sub.south, sub.east, sub.north]
            )
            sub_prefix = f"{prefix}_tile_{k:04d}"
            tile_paths.append(
                self._download_one_url_tile(image, sub_region, scale, sub_prefix)
            )
        target = self.root_dir / f"{prefix}.tif"
        merge_rasters([str(p) for p in tile_paths], str(target))
        for p in tile_paths:
            p.unlink(missing_ok=True)
        logger.info(f"Stitched {len(tile_paths)} tile(s) into {target} via pyramids.")
        return target

    def _export_via_batch(
        self, image, scale: float, region, prefix: str
    ) -> str | TaskInfo:
        """Queue an `ee.batch.Export.image.to{Drive,CloudStorage,Asset}` task.

        When `wait_for_export=True` (the default) blocks until the task
        reaches a terminal state via `wait_for_task` and returns the
        destination URL (`drive://...` / `gs://...` / `ee://...`).
        When `wait_for_export=False` returns a :class:`TaskInfo`
        immediately so the caller can track the task asynchronously via
        :mod:`earthlens.gee.jobs`.
        """
        common = {
            "image": image,
            "description": prefix[:100],
            "region": region,
            "scale": scale,
            "crs": self.crs,
            "maxPixels": 1e13,
        }
        if self.export_via == "drive":
            task = ee.batch.Export.image.toDrive(
                folder=self.drive_folder, fileNamePrefix=prefix, **common
            )
            destination = f"drive://{self.drive_folder}/{prefix}"
        elif self.export_via == "gcs":
            task = ee.batch.Export.image.toCloudStorage(
                bucket=self.gcs_bucket, fileNamePrefix=prefix, **common
            )
            destination = f"gs://{self.gcs_bucket}/{prefix}"
        else:
            # The asset sink uses `assetId` instead of `fileNamePrefix` —
            # each export creates one asset at `<self.asset_id>/<prefix>`.
            assert (
                self.asset_id is not None
            )  # constructor requires it for export_via='asset'
            target_asset = f"{self.asset_id.rstrip('/')}/{prefix}"
            task = ee.batch.Export.image.toAsset(assetId=target_asset, **common)
            destination = f"ee://{target_asset}"
        if not self.wait_for_export:
            task.start()
            info = _op_to_taskinfo(task.status())
            logger.info(
                f"Submitted {self.export_via} export {info.id} "
                f"({info.description}); track via earthlens.gee.jobs."
            )
            return info
        wait_for_task(task, progress_bar=True)
        logger.info(
            f"Exported {destination} (pull it from the {self.export_via} destination)"
        )
        return destination

    def _ee_region(self):
        """Return the `ee.Geometry` to clip / filter requests to.

        Uses the constructor `region` `GeoDataFrame` (converted via
        :func:`earthlens.gee.features.create_feature`) when given, else a
        polygon `aoi=` carried on `self.space.geometry` (the unified
        ergonomic channel), and otherwise an `ee.Geometry.Rectangle` built
        from the lat/lon bbox. Computed once and cached.

        Returns:
            The `ee.Geometry`.
        """
        if self._ee_geometry is None:
            aoi_geometry = getattr(self.space, "geometry", None)
            if self.region is not None:
                self._ee_geometry = create_feature(self.region).geometry()
            elif aoi_geometry is not None:
                self._ee_geometry = create_feature(aoi_geometry).geometry()
            else:
                self._ee_geometry = ee.Geometry.Rectangle(
                    [
                        self.space.longitude_min,
                        self.space.latitude_min,
                        self.space.longitude_max,
                        self.space.latitude_max,
                    ]
                )
        return self._ee_geometry

    def _clamp_window_to_extent(
        self, var_info: Dataset
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Clamp the request window to a dataset's published extent.

        Args:
            var_info: The catalog entry (its :class:`Extent`).

        Returns:
            `(start, end_exclusive)` — `start` is the later of the
            request start and the dataset start; `end_exclusive` is the
            earlier of (request end + 1 day) and (dataset end + 1 day, or
            "now" + 1 day for open-ended datasets). Returns
            `(None, None)` if the windows do not overlap.

            When `discover_extent=True` was passed at construction
            and the catalog's `end_date` (or `start_date`) is missing,
            the gap is filled by an EE-side
            `reduceColumns(minMax)` over `system:time_start` via
            :meth:`_discover_ee_extent` (cached per asset for the
            lifetime of the instance).
        """
        req_start = self.time.start_date
        req_end_excl = self.time.end_date + dt.timedelta(days=1)

        ds_start, ds_end_excl = self._effective_extent(var_info)

        start = max(req_start, ds_start)
        end_excl = min(req_end_excl, ds_end_excl)
        if start >= end_excl:
            return None, None
        return start, end_excl

    def _effective_extent(self, var_info: Dataset) -> tuple[dt.datetime, dt.datetime]:
        """Resolve a dataset's effective `(start, end_exclusive)` extent.

        The catalog's `start_date` is always a curated string (the
        `Extent` pydantic field is required); the upper bound comes
        from the curated `end_date` if present, else — when
        `discover_extent=True` — an EE-side `reduceColumns(minMax)`
        query (cached per asset), falling back to `now() + 1 day` if
        the query fails or the catalog has no `end_date` and
        discovery is disabled.

        Args:
            var_info: The catalog entry.

        Returns:
            `(start, end_exclusive)` as naive UTC datetimes.
        """
        ds_start = dt.datetime.strptime(var_info.extent.start_date, "%Y-%m-%d")
        catalog_end_str = var_info.extent.end_date

        if catalog_end_str is not None:
            ds_end_excl = dt.datetime.strptime(
                catalog_end_str, "%Y-%m-%d"
            ) + dt.timedelta(days=1)
            return ds_start, ds_end_excl

        _, ee_end = self._maybe_discover_ee_extent(var_info)
        if ee_end is not None:
            return ds_start, ee_end + dt.timedelta(days=1)

        # `now()` would be local-naive; the rest of the path is naive
        # UTC, so use a naive UTC value.
        ds_end_excl = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(
            days=1
        )
        return ds_start, ds_end_excl

    def _maybe_discover_ee_extent(
        self, var_info: Dataset
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Cached entry point for :meth:`_discover_ee_extent`.

        Reads / writes :data:`_EXTENT_CACHE` (module-level) so a
        second `GEE(...)` instance querying the same asset doesn't
        re-issue the 2-5 s `reduceColumns(minMax)` round trip.
        """
        if not self.discover_extent:
            return None, None
        cached = _EXTENT_CACHE.get(var_info.id)
        if cached is not None:
            return cached
        discovered = self._discover_ee_extent(var_info)
        _EXTENT_CACHE[var_info.id] = discovered
        return discovered

    def _discover_ee_extent(
        self, var_info: Dataset
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Query a collection's actual `system:time_start` min/max via EE.

        Issues one `reduceColumns(ee.Reducer.minMax(), ["system:time_start"])
        .getInfo()` round-trip per asset (callers cache via
        :meth:`_maybe_discover_ee_extent`). On any EE-side failure
        (network, missing property, image-typed asset) returns
        `(None, None)` and logs a warning — the caller falls back to
        the catalog values or `now()`.

        Args:
            var_info: The catalog entry. Only `var_info.id` is used.

        Returns:
            `(min_dt, max_dt)` as naive UTC datetimes, or `(None,
            None)` if the query failed or the collection has no
            time-stamped images.
        """
        try:
            collection = ee.ImageCollection(var_info.id)
            result = (
                collection.reduceColumns(
                    ee.Reducer.minMax(), ["system:time_start"]
                ).getInfo()
                or {}
            )
        except Exception as exc:  # noqa: BLE001 - downgrade EE errors to a warning
            logger.warning(
                f"discover_extent: reduceColumns(minMax) failed for "
                f"{var_info.id}: {type(exc).__name__}: {exc}; "
                "falling back to catalog / now()."
            )
            return None, None

        min_ms = result.get("min")
        max_ms = result.get("max")
        if min_ms is None or max_ms is None:
            return None, None
        return (
            dt.datetime.fromtimestamp(min_ms / 1000.0, tz=dt.UTC).replace(tzinfo=None),
            dt.datetime.fromtimestamp(max_ms / 1000.0, tz=dt.UTC).replace(tzinfo=None),
        )
