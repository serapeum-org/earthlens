"""Google Earth Engine backend — `GEE`, an `AbstractDataSource`.

Downloads imagery from Google Earth Engine. A request is `{asset_id:
[band, ...], ...}` (the addressable units of an EE dataset are *bands*,
and one image carries many at once), plus a date range, a bbox (or a
`GeoDataFrame` region), a temporal-compositing resolution
(`"raw"`/`"daily"`/`"monthly"`/`"yearly"`), and an output pixel `scale`
in metres. The asset ids and band metadata are resolved through
`earthlens.gee.Catalog` (loaded from the per-category YAMLs
under `src/earthlens/gee/catalog/`).

Per `(asset, band-set, time-bucket)` the pipeline is:

* `_build_collection` — `ee.ImageCollection(asset_id)` (or the
  single `ee.Image` wrapped in one), `.filterDate(...)`,
  `.filterBounds(region)`, then any constructor `filters` and the
  per-image `cloud_mask` (`.map`-applied), and finally `.select(bands)`.
  Pure: no I/O.
* `_composite` — split the request window into buckets at the
  requested cadence and collapse each with the dataset's
  `default_reducer` (or the constructor `reducer` override) — `mean`
  for continuous fields / rates, `median` for cloud-screened optical
  scenes, `mosaic` for tiled / annual static maps. Yields one
  `ee.Image` per bucket.
* the EEDAI fast-path — a raw read of a materialised asset, or a
  client-side composite of an `ImageCollection`, served by the pyramids-eo
  reader in EPSG:4326 or a metre-based projected CRS; see
  `_eedai_eligible` for what qualifies and `_eedai_verdict` for
  what it costs.
* `_api` — export the bucket image via the configured
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
Cloud project, performed by `_initialize` via
`earthlens.gee.auth.EarthEngineAuth` (service-account key) or, if
no key is given, an interactive `ee.Authenticate()` against an explicit
`project`. Credential / registration failures surface as
`AuthenticationError`.
"""

from __future__ import annotations

import datetime as dt
import gc
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

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
    credentials_for,  # noqa: F401 - monkeypatch seam the test suite patches
    eedai_available,  # noqa: F401 - monkeypatch seam the test suite patches
)
from earthlens.gee._helpers import (
    _COLLECTION_REDUCERS,
    EE_MAX_DIMENSION,
    slug_asset_id,
    split_aoi_for_url,
    wait_for_task,
)
from earthlens.gee.auth import AuthenticationError, EarthEngineAuth, _redact
from earthlens.gee.catalog import Catalog, Dataset
from earthlens.gee.configs import EngineConfig, ExportConfig
from earthlens.gee.engines import (
    NO_SCENES,
    EedaiEngine,
    reader_refusal_types,
    resolve_engine,
)
from earthlens.gee.features import create_feature
from earthlens.gee.grid import (
    OutputGrid,
    bbox_to_crs,
    geographic_grid,
    same_crs,
    validate_crs_transform,
)
from earthlens.gee.jobs import TaskInfo, _op_to_taskinfo
from earthlens.gee.pixels import PixelsEngine
from earthlens.gee.plans import EedaiPlan, EnginePlan
from earthlens.gee.reader import EedaiReader
from earthlens.gee.reducers import collection_reducers, resolve_reducer
from earthlens.gee.sinks import AssetSink, _BatchSink, resolve_sink
from earthlens.gee.work import (
    Artifact,
    DownloadResult,
    FeatureWorkUnit,
    RasterWorkUnit,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas import GeoDataFrame

    from earthlens.base.http import HttpClient
    from earthlens.gee.analysis.base import OperationChain, RasterOperation
    from earthlens.gee.sampling.base import Sampler

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
#: materialise in one piece. It is only a ceiling: `GEE._eedai_single_image_plan`
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

#: Total pixels one synchronous `getDownloadURL` fetch is sized against. Earth
#: Engine caps the response itself; this is only used to phrase the per-axis
#: refusal, so it is the per-axis cap squared rather than a documented limit.
_EE_URL_MAX_PIXELS: int = EE_MAX_DIMENSION * EE_MAX_DIMENSION

#: Marks a decline caused by the *data* rather than the request, so a forced
#: `engine="eedai"` skips the bucket instead of failing the whole download.
#: Aliases `engines.NO_SCENES` rather than repeating the string: the skip is a
#: prefix match, so two independent literals would let one drift and silently
#: turn the bucket-skip into a hard failure.
_EEDAI_NO_SCENES: str = NO_SCENES

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
#: longitude is scaled by `cos(latitude)` at the AOI's equatorward edge.
_METRES_PER_DEGREE: float = 111_320.0


def __getattr__(name: str) -> frozenset[str]:
    """Keep `_ENGINES` readable as a module attribute.

    Args:
        name: The attribute being looked up.

    Returns:
        The derived engine-name set for `_ENGINES`.

    Raises:
        AttributeError: For any other name.
    """
    if name == "_ENGINES":
        return _engine_names()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _engine_names() -> frozenset[str]:
    """Return the engine names `engine=` accepts, from the one registry.

    Derived from `earthlens.gee.engines.ENGINES` rather than restated, so
    registering an engine is enough to make it selectable - which is what Task
    1.13's `PixelsEngine` relies on.

    Returns:
        Every registered engine name.
    """
    from earthlens.gee import pixels  # noqa: F401 - registers PixelsEngine
    from earthlens.gee.engines import selectable_engines

    return selectable_engines()


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
# in `catalog.py`; clear it via `clear_extent_cache` (mostly for
# tests).
_EXTENT_CACHE: dict[str, tuple[dt.datetime | None, dt.datetime | None]] = {}


def clear_extent_cache() -> None:
    """Forget every cached EE-discovered temporal extent.

    Mirrors `earthlens.gee.catalog.clear_catalog_cache` for the
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
    helper duplicates the cheap checks from `_check_input_dates`
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

    Same lingering-handle hazard as `_rename_when_unlocked`, but the
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


def _consume_quoted(text: str, index: int, quote: str) -> tuple[int, str | None]:
    """Advance past one character inside a quoted literal.

    Args:
        text: The whole filter.
        index: The character being consumed.
        quote: The quote character that opened the literal.

    Returns:
        The next index, and the quote still in effect (`None` once it closed).
    """
    if text[index] != quote:
        return index + 1, quote
    # A doubled quote is an escaped one, not the end of the literal.
    if text[index + 1 : index + 2] == quote:
        return index + 2, quote
    return index + 1, None


def _scan_property_filter(text: str) -> tuple[int, str | None]:
    """Walk a filter, raising on structure that would escape the reader's wrapper.

    Args:
        text: The stripped filter.

    Returns:
        The final parenthesis depth, and the quote left open (`None` when every
        literal was closed).

    Raises:
        ValueError: It closes a parenthesis it never opened, or carries a bare
            statement separator or SQL comment outside a literal.
    """
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        if quote is not None:
            index, quote = _consume_quoted(text, index, quote)
            continue
        char = text[index]
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" or text.startswith("--", index):
            raise ValueError(
                "property_filter must be a single expression; remove the "
                f"statement separator or comment from {text!r}"
            )
        if depth < 0:
            raise ValueError(
                "property_filter closes a parenthesis it never opened, which "
                f"would escape the filter it is combined with: {text!r}"
            )
        index += 1
    return depth, quote


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

    depth, quote = _scan_property_filter(text)
    if quote is not None:
        raise ValueError(
            f"property_filter has an unterminated quote: {property_filter!r}"
        )
    if depth:
        raise ValueError(
            f"property_filter leaves {depth} parenthesis/es unclosed: {property_filter!r}"
        )


def _declaring(reader: Any, nodata: float | int) -> Any:
    """Return an in-memory copy of `reader` that declares `nodata`.

    Earth Engine writes no no-data value into the GeoTIFF it returns, and a fill
    value nothing declares is just data: a `-9999` ocean pixel folds into every
    mean and every colour ramp. pyramids has no way to stamp a declaration onto
    an existing raster without also rewriting its pixels - `_set_no_data_value`
    fills the whole band with the value, and `change_no_data_value` does not
    persist - so the declaration is made where pyramids does accept one, at
    construction.

    Args:
        reader: The pyramids `Dataset` holding the fetched pixels.
        nodata: The fill value to declare.

    Returns:
        A new in-memory `Dataset` over the same array, georeferencing and CRS,
        with `nodata` declared. The caller closes it.
    """
    from pyramids.base.georeference import GeoReference

    return PyramidsDataset.from_array(
        reader.read_array(),
        geo_ref=GeoReference(geo=reader.geotransform, epsg=reader.epsg),
        no_data_value=nodata,
    )


def _eedai_remedy(reason: str) -> str:
    """Suggest what to change, given why the reader declined.

    Routing collections here made the decline reasons stop being only about
    size, so the one fixed "use a smaller bbox" suffix started pointing at the
    wrong dial - nothing about a bbox fixes an unsupported reducer or a missing
    credential. Matched most specific first, since the reasons overlap in
    wording (a stack over the pixel budget also names its scene count).

    Args:
        reason: The declined plan's reason.

    Returns:
        A remedy sentence to append to the forced-engine error.
    """
    if "last-wins" in reason:
        return (
            "Pass a statistical reducer (mean/median/min/max/sum), or "
            "engine='ee' to keep Earth Engine's mosaic semantics."
        )
    if "not in the reader's vocabulary" in reason:
        return (
            "Pass one of the reducers the reader knows "
            "(mean/median/min/max/mode/sum), or engine='ee', which can express "
            "any of them."
        )
    if "credential" in reason:
        return "Fix the service key or service account, or use engine='ee'."
    if "no native resolution" in reason:
        return (
            "The catalog row has no resolution to size a read from; use "
            "engine='ee', or curate the dataset's spatial_resolution."
        )
    if "single-pass budget" in reason:
        return (
            "Use a coarser scale, a smaller bbox, a shorter bucket "
            "(temporal_resolution), or engine='ee'."
        )
    if "scene cap" in reason:
        return (
            "Narrow the date window, shorten the bucket (temporal_resolution), "
            "add a property_filter, or use engine='ee' so Earth Engine reduces "
            "the scenes server-side."
        )
    if "cutline" in reason:
        return "Drop the region cutline, or use engine='ee'."
    if "resample" in reason:
        return f"Use resample={_EEDAI_TILING_RESAMPLE!r}, or engine='ee'."
    return (
        "Use a smaller bbox, a coarser scale, engine='ee' (with auto_split=True "
        "to tile), or export_via='drive'."
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
    # `ValueError` is deliberately absent: upstream raises it for an invalid
    # *option combination*, which means this backend built the call wrongly — a
    # bug to surface, not a transient to fall back from.
    errors: list[type[BaseException]] = [OSError]
    candidates = [getattr(reader, "ReaderError", None)]
    try:
        from pyramids_eo.errors import ReaderError

        candidates.append(ReaderError)
    # Reached whenever the `[eedai]` extra is absent, which is a supported
    # lane - `earthlens[all]` omits it, so the wheel job runs here - but not
    # one coverage is measured in, since every dev install pins pyramids-eo.
    except ImportError:  # pragma: no cover - needs the extra uninstalled
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


class GEE(LazyClientMixin, AbstractDataSource):
    """Google Earth Engine data source.

    Args:
        start: Inclusive start date string (parsed with `fmt`).
        end: Inclusive end date string.
        variables: Mapping `{asset_id: [band, ...]}` — each `asset_id`
            must be a key of `Catalog.datasets` and each band a
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
        crs: Output CRS, in any form pyproj accepts. Defaults to
            `"EPSG:4326"`. This also selects the EEDAI fast path: it serves
            EPSG:4326 and any projected CRS whose axis unit is metres (because
            `scale` is metres), and declines anything else — a non-4326
            geographic CRS, or a projected one in feet. Declining is quiet
            under `engine="auto"` (Earth Engine serves the request instead) and
            an error under `engine="eedai"`.
        reducer: Override the per-dataset `default_reducer` for the
            temporal composite (`mean` / `median` / `min` / `max` /
            `mode` / `mosaic` / `sum`, or `percentile:<q>`). `None` (the
            default) uses each dataset's own `default_reducer`. The registry
            also holds the region-only reducers `first` / `stdDev` /
            `variance` / `count`, which collapse pixels inside a region rather
            than images in a collection; naming one here is refused at
            construction, since there is nothing for it to composite. Pass
            those to `sample_points` instead.
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
        auto_split: For `export_via="url"`, when the request exceeds Earth
            Engine's 32768-px per-axis cap, automatically split the AOI into
            tiles each within the cap, download each tile separately, and
            mosaic them back into a single GeoTIFF via
            `pyramids.dataset.merge.merge_rasters`. Applies to both pull APIs
            — `engine="ee"` and `engine="pixels"` share that cap — and means
            nothing to `engine="eedai"`, which has none. Defaults to `False` —
            the previous behaviour, which raises `ValueError` with an
            actionable message.
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
            `download()` returns a list of `TaskInfo` objects
            so the caller can track them asynchronously via
            `earthlens.gee.jobs`. Ignored for `export_via="url"`,
            which is always synchronous.
        cloud_mask: Optional per-image mask `.map`-applied to every image
            in the stack *before* the reducer, so the composite is built
            from cloud-screened pixels — the usual way to get a clean
            optical mosaic. A callable `ee.Image -> ee.Image`; see
            `earthlens.gee.cloud_masks` (`landsat_sr` /
            `sentinel2_scl`). It runs before the band `select`, so it may
            read quality bands (`QA_PIXEL` / `SCL`) that are not listed in
            `variables`. Meant for image collections; on a static
            `ee_type="image"` dataset it is applied verbatim and a warning
            is logged (see `_build_collection`). Defaults to `None`
            (no masking).
        filters: Optional iterable of `ee.ImageCollection ->
            ee.ImageCollection` filters applied to the stack after the
            spatial / temporal filters (`filterBounds`, and `filterDate`
            for image collections) and before the `cloud_mask` and
            reducer — e.g. a metadata cloud-cover cap. Each entry takes
            the collection and returns it; wrap the
            `earthlens.gee.filters` helpers (`by_cloud_cover_lte` /
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
            it back to Earth Engine. One difference is worth knowing when a
            collection *is* served: the reader reduces client-side with no
            nodata value, because the EEDAI driver declares none, so a scene's
            fill pixels are folded into a `mean` / `median` / `min` / `max` /
            `sum` composite where Earth Engine would have masked them. The two
            agree wherever the scenes carry no fill over the AOI; pass
            `engine="ee"` when you need Earth Engine's masking exactly. It
            falls back to Earth Engine's
            `getDownloadURL` otherwise. `"ee"` always uses `getDownloadURL`
            (the historical behaviour). `"pixels"` uses
            `ee.data.computePixels`, Earth Engine's current extraction API: it
            evaluates the same graph (so a `cloud_mask` / `filters` survives)
            and returns the GeoTIFF bytes directly from an explicit grid, with
            no URL to expire and no zip to unpack. It is bound by the same
            32768-px per-axis cap as `getDownloadURL` — measured, not assumed —
            so `auto_split=True` tiles for it exactly as it does there. It is
            never chosen by `"auto"`; ask for it by name. `"eedai"` forces the
            reader and raises if the request is not eligible. The EEDAI path
            reads pixels straight from the asset, so Earth Engine's 32768-px
            synchronous cap does not apply and `auto_split` is unnecessary —
            a window too large to materialise is streamed to disk in tiles
            and mosaicked. It cannot run server-side compute, which is why
            composited requests stay on Earth Engine. Ignored for the
            asynchronous `"drive"` / `"gcs"` / `"asset"` sinks, which are
            Earth Engine-only.

            The engines do not produce byte-identical rasters. `"ee"` reads
            `scale` in a geographic CRS as a uniform degree-equivalent, while
            the EEDAI grid — and the explicit grid `"pixels"` sends — is sized
            for square metres on the ground, so away from the equator the
            column counts differ; and the reader downsamples locally (nearest
            by default) where Earth Engine aggregates server-side. The AOI, CRS
            and values agree — the sampling does not. Pass `crs_transform=`
            when the grid itself has to match across engines or across
            downloads. `"eedai"` still needs the `[gee]` extra and Earth Engine
            credentials: the request is built through `ee` before the pixels
            are fetched.
        cog: Write the raster as a Cloud Optimized GeoTIFF (tiled, with
            overviews) via `Dataset.cog.to_cog` instead of a plain GeoTIFF.
            Applies to the paths that write the file themselves — the EEDAI
            reader and `engine="pixels"`. `getDownloadURL` streams Earth
            Engine's own bytes and the batch sinks write server-side, so both
            ignore it. Defaults to `False`.
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
        crs_transform: A 6-element affine transform
            `(x_scale, x_shear, x_origin, y_shear, y_scale, y_origin)`, in the
            units of `crs`, pinning the output grid's pixel size *and* origin.
            Earth Engine treats it as an alternative to `scale` rather than an
            addition, so a pinned request sends the transform and no scale: two
            downloads over different AOIs then land on the same pixel
            boundaries and differencing them needs no resample. Honoured on
            every Earth Engine path — `getDownloadURL` and the batch sinks
            take it directly, and `engine="pixels"` sends it as the request's
            `affineTransform`. The EEDAI reader sizes its own metre grid and
            cannot pin one, so a pinned request is never routed to it and
            `engine="eedai"` raises. A resolvable `scale` (from `scale=` or the
            catalog row) is still required, because the routing and naming
            decisions are made on it; it is simply not sent. Defaults to
            `None`.
        nodata: The fill value to declare on the fetched raster. Only the pixel
            API's request body can carry one — `getDownloadURL` has no such
            argument and the batch sinks declare it through their own format
            options — so it applies to `engine="pixels"` and is ignored, with a
            warning, anywhere else. Defaults to `None`.
        high_volume: Initialise Earth Engine against the
            [high-volume endpoint](https://developers.google.com/earth-engine/cloud/highvolume)
            (`https://earthengine-highvolume.googleapis.com`), which is
            documented for many parallel requests. The trade-off is real: less
            caching, and complex queries cost more EECU-time there than on the
            regular endpoint, so it is opted into per backend rather than
            switched on globally. Only `engine="pixels"` uses it, because the
            pixel API is what the endpoint is documented for; ignored, with a
            warning, elsewhere. `ee` holds one session per process, so the last
            backend to open its connection decides the host for every backend
            in that process. Defaults to `False`.

    Credentials are not constructor arguments — the constructor describes
    only what to fetch. Supply them at the authentication step:
    `authenticate` accepts `service_account=` / `service_key=` /
    `project=`, each falling back to the `GEE_SERVICE_ACCOUNT` /
    `GEE_SERVICE_KEY` / `GEE_PROJECT` environment variable when omitted.
    `download()` opens the connection lazily (resolving the same way) if
    `authenticate()` was never called.

    Raises:
        AuthenticationError: If Earth Engine cannot be initialised
            (missing/invalid key, unregistered project, missing IAM role).
        ValueError: At construction for a bad `export_via` (or `"drive"`
            without `drive_folder` / `"gcs"` without `gcs_bucket` /
            `"asset"` without `asset_id`), a `crs_transform` that is not six
            finite terms with non-zero, unsheared pixel sizes, or a `reducer`
            that reduces a region rather than a collection; from the parent on
            a bad date range; from `_check_input_dates` on an unknown
            `temporal_resolution`; from `_api` on a missing scale or an
            oversized `"url"` request on either pull API (unless
            `auto_split=True`); from `_download_dataset` on an unknown
            asset id or band.
        TypeError: At construction when `cloud_mask` is not callable, when
            `filters` is a `str` / bytes-like / mapping / non-iterable or
            contains a non-callable entry, or when `crs_transform` is not an
            iterable of numbers.
        NotImplementedError: From `download` when `aggregate=` is
            passed (not yet supported).
        RuntimeError: From `_api` if a `"drive"` / `"gcs"` export
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
        """The bundled GEE `Catalog` (alias of `_catalog`)."""
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
        engine: Literal["auto", "ee", "eedai", "pixels"] = "auto",
        cog: bool = False,
        resample: str = "nearest",
        property_filter: str | None = None,
        materialize: bool = False,
        crs_transform: Sequence[float] | None = None,
        nodata: float | int | None = None,
        high_volume: bool = False,
        sampler: Sampler | None = None,
        geometry: GeoDataFrame | None = None,
    ):
        # Validate the cheap (no-I/O) config first so user typos surface
        # before the ~3.3 s cold-cache catalog parse below.
        # Resolved, not matched against a restated set: the literal vocabulary
        # here was the reason a newly registered sink was still rejected, which
        # made the "adding a sink needs no backend edit" claim false.
        sink = resolve_sink(export_via)
        if property_filter is not None:
            _validate_property_filter(property_filter)
        # Both inputs are known now, so refuse now: checking after `download()`
        # meant every export in the run completed - and, with the default
        # `wait_for_export=True`, was polled to completion - before raising,
        # losing the destinations it had just produced.
        if materialize and sink.is_batch:
            raise ValueError(
                f"materialize=True needs pixels on disk, but export_via="
                f"{export_via!r} queues the work remotely and returns a "
                "destination. Use export_via='url', or pull the result from "
                "the destination yourself."
            )
        # The sink refuses itself, so this function knows no destination by
        # name: it hands over the candidate values and lets the resolved sink
        # say whether the one it needs is there.
        sink.validate_config(
            {
                "drive_folder": drive_folder,
                "gcs_bucket": gcs_bucket,
                "asset_id": asset_id,
            }
        )
        if cloud_mask is not None and not callable(cloud_mask):
            raise TypeError(
                "cloud_mask must be a callable ee.Image -> ee.Image (or None), "
                f"got {type(cloud_mask).__name__}"
            )
        collection_filters = _validate_filters(filters)
        pinned_transform = validate_crs_transform(crs_transform)
        # Resolved here, not only at composite time: `reducer=` names one
        # vocabulary now, and four of its members reduce pixels in a region
        # rather than images in a collection. Refusing at construction keeps
        # that distinction a config error like every other one, instead of a
        # failure part-way through a run that has already written buckets.
        if reducer is not None and not resolve_reducer(reducer).collection_capable:
            raise ValueError(
                f"reducer={reducer!r} reduces pixels in a region, not images in "
                "a collection, so it cannot composite a time bucket; use one of "
                f"{sorted(collection_reducers())}, 'percentile:<q>', or pass it "
                "to sample_points(reducer=...) instead."
            )
        # Validated by `resolve_engine`, not re-checked here: it owns the one
        # message that can tell "no such engine" apart from "registered but not
        # selectable yet", and a second copy of the vocabulary is what let the
        # two drift in the first place.
        resolved_engine = resolve_engine(engine)
        # `nodata` and `high_volume` are expressible only through the pixel
        # API's request body and endpoint. Warned about here rather than at
        # export time: both are decided by `engine=`, which is known now, so
        # the notice arrives while the caller can still act on it. A warning
        # rather than a refusal, because it is how `cog` and `property_filter`
        # already report being on the wrong path, and because ignoring the
        # argument still produces the pixels that were asked for.
        # Earth Engine takes a fill value in exactly two places, and they are
        # not the ones the first pass assumed. Its `PixelGrid` has no nodata
        # field at all - a request carrying one is refused with `Unknown name
        # "nodata" at 'grid'` - so the pixel path has to unmask the expression
        # and declare the value on the raster it writes itself. The only place
        # the API accepts one directly is `GeoTiffImageExportOptions.noData`,
        # i.e. a file-based batch export. Everything else ignores it: an asset
        # export has no file format, and `getDownloadURL` has no such argument.
        # Judged on the resolved engine, not the argument, because `engine=`
        # also takes an `Engine` instance.
        honours_nodata = (
            resolved_engine.name == PixelsEngine.name and not sink.is_batch
        ) or sink.takes_format_options
        if nodata is not None and not honours_nodata:
            logger.warning(
                f"nodata={nodata} is honoured by engine='pixels' with "
                "export_via='url' (unmasked server-side, declared on the "
                "written raster) and by export_via='drive'/'gcs' (Earth "
                f"Engine's own GeoTIFF option); this request "
                f"(engine={resolved_engine.name!r}, "
                f"export_via={export_via!r}) ignores it."
            )
        if high_volume and resolved_engine.name != PixelsEngine.name:
            logger.warning(
                "high_volume=True is only used by the pixel API, so "
                f"engine={resolved_engine.name!r} ignores it and initialises "
                "against the regular endpoint; pass engine='pixels' to use it."
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
        #: before the reducer in `_build_collection`.
        self.cloud_mask = cloud_mask
        #: The validated `filters` as a tuple (empty when none were given),
        #: applied left to right in `_build_collection`.
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
        #: The validated 6-element transform pinning the output grid, or `None`.
        self.crs_transform: tuple[float, ...] | None = pinned_transform
        #: Fill value declared on the pixel API's request, or `None`.
        self.nodata = nodata
        #: Initialise against Earth Engine's high-volume endpoint.
        self.high_volume = bool(high_volume)
        # Composed from the same kwargs the public attrs above carry, so a
        # strategy receives a validated slice rather than the whole backend.
        # The public attrs stay as the back-compat surface.
        # Resolved once: `engine=` is validated above, and the object replaces
        # the `if/elif` that used to re-read the string at every decision.
        self._engine = resolved_engine
        #: The resolved destination; `export_via=` is validated above.
        self._sink = resolve_sink(export_via)
        #: Return in-memory pyramids objects from `download()` as well as paths.
        self.materialize = bool(materialize)
        self._export_config = ExportConfig(
            export_via=export_via,
            drive_folder=drive_folder,
            gcs_bucket=gcs_bucket,
            asset_id=asset_id,
            cog=bool(cog),
            resample=resample,
            auto_split=bool(auto_split),
            http_timeout=self.http_timeout,
            wait_for_export=bool(wait_for_export),
            materialize=bool(materialize),
            nodata=nodata,
        )
        self._ee_geometry: Any = None  # lazily built in `_ee_region`
        self._eedai_credential: Any = None  # lazily built in `_eedai_credentials`
        #: The EEDAI adapter, lazily built in `_eedai_reader`.
        self._eedai_reader_adapter: EedaiReader | None = None
        #: Region reprojections, keyed by target CRS. A large `GeoDataFrame` on
        #: a many-bucket run would otherwise be warped once per bucket per use.
        self._region_cache: dict[str, Any] = {}
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

        # Epic 3 vector/tabular output: when a `sampler` is supplied, each
        # composited bucket is sampled into an `ee.FeatureCollection` rather
        # than exported as a raster, and the instance reports its output kind
        # from the sampler (the jrc backend also overrides OUTPUT_KIND per
        # instance). With no sampler the class default `"raster"` and the whole
        # existing download path are untouched.
        self._sampler = sampler
        self._geometry = geometry
        if sampler is not None:
            if geometry is None:
                raise ValueError(
                    "a sampler needs a geometry to sample over; pass geometry=..."
                )
            self.OUTPUT_KIND = cast("OutputKind", sampler.output_kind)

    def _resolve_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Resolve credentials from explicit values, then the environment.

        Each credential piece falls back to its environment variable when
        not set explicitly (via `authenticate`): `GEE_SERVICE_ACCOUNT`,
        `GEE_SERVICE_KEY`, `GEE_PROJECT`.

        Returns:
            tuple: `(service_account, service_key, project)`, each `None`
                when neither an explicit value nor its env var is set.

        Examples:
            - Explicit values (set by `authenticate`) are returned as-is:
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
        # The adapter holds the credential, so it has to drop with it or a
        # re-authentication would keep reading as the previous identity.
        self._eedai_reader_adapter = None
        # LazyClientMixin: first access to `client` runs `_open_client` (auth).
        _ = self.client
        return self

    def _open_client(self) -> Any:
        """Authenticate and initialise the Earth Engine connection (lazily).

        Resolves the credentials (explicit values from `authenticate`,
        else the `GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY` / `GEE_PROJECT`
        environment variables), then uses a service-account key when a
        `service_account` + `service_key` pair is available (via
        `EarthEngineAuth`); otherwise runs `ee.Authenticate()` and
        `ee.Initialize(project=...)` against the resolved `project`. The host is
        whatever the resolved engine's `endpoint`
        returns for this backend's `high_volume` setting — the default for every
        engine but the pixel API's, which can be pointed at the high-volume
        endpoint. `ee` keeps one session per process, so the last backend to
        open its connection decides the host for the others too. The
        `ee.Authenticate()` flow is interactive — it opens a browser and
        waits for the user to paste a token, so on a headless box (CI,
        Docker, remote shell) it will hang or fail with whatever the EE
        SDK emits natively; use service-account auth for non-interactive
        use. The resolved project id is stored on `project`. Called
        by `client` on first use.

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
        # The resolved engine decides the host: only the pixel API documents
        # an alternative one, and `Engine.endpoint` answers `None` for the rest,
        # so this reads the same for every other engine as it did before.
        host = self._engine.endpoint(self._engine_config.high_volume)
        if service_account and service_key:
            self.project = EarthEngineAuth.initialize(
                service_account, service_key, project, host=host
            )
            return ee
        host_kwargs = {"opt_url": host} if host else {}
        try:
            ee.Authenticate()
            ee.Initialize(project=project, **host_kwargs)
        except ee.EEException as exc:
            # `ee.Authenticate()` falls back to application-default credentials,
            # and an ADC file is an `authorized_user` JSON - a credential shape
            # with no PEM armour whose `client_secret` and `refresh_token` would
            # otherwise reach the log verbatim. Usually nothing was resolved to
            # substitute here - this branch is taken when the service-account
            # pair is incomplete - so it is `_redact`'s marker check that does
            # the work; a key resolved without an account email does land here,
            # so whatever was resolved is still handed over. Classify on the raw
            # text, report the redacted one, and break the chain so the cause
            # cannot print.
            raw = str(exc)
            message = _redact(raw, service_key or "")
            if "not registered to use Earth Engine" in raw:
                raise AuthenticationError(
                    f"Cloud project {project!r} is not registered to use "
                    "Earth Engine. Register it at "
                    "https://code.earthengine.google.com/register, then retry."
                ) from None
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project {project!r}: {message}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project {project!r}: "
                f"{_redact(str(exc), service_key or '')}"
            ) from None
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
            string passed in), and `dates` — a `pandas.DatetimeIndex`
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

    def download(
        self, progress_bar: bool = True, *, materialize: bool | None = None
    ) -> list[Path | str | TaskInfo | GeoDataFrame] | DownloadResult:
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
                oversized `"url"` request (see `_api`).
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
                `ee.Initialize` used by `_open_client`.
        """
        # Before anything is exported: the refusal used to arrive after every
        # bucket had been written and polled to completion, which threw away
        # the destinations it had just produced.
        if (self.materialize if materialize is None else materialize) and (
            self._sink.is_batch
        ):
            raise ValueError(
                f"materialize=True needs pixels on disk, but export_via="
                f"{self.export_via!r} queues the work remotely and returns a "
                "destination. Use export_via='url', or pull the result from "
                "the destination yourself."
            )
        # Trigger the lazy Earth Engine auth/init before any `ee` call.
        _ = self.client
        self._cog_warned = False  # the cog= notice is once per run, not per object
        self._property_filter_warned = set()  # same, for the property_filter notice
        outputs: list[Path | str | TaskInfo | GeoDataFrame] = []
        assert isinstance(
            self.vars, dict
        )  # GEE always uses the {asset_id: [band]} form
        for asset_id, bands in self.vars.items():
            outputs.extend(self._download_dataset(asset_id, list(bands), progress_bar))
        written = outputs
        # The vector/tabular path already holds in-memory GeoDataFrames (or
        # table TaskInfos), so it never goes through `_materialise`, which reads
        # written raster paths back off disk.
        if self._sampler is not None:
            return written
        if not (self.materialize if materialize is None else materialize):
            return written
        return self._materialise(written)

    def analyze(
        self,
        ops: RasterOperation | OperationChain | Sequence[RasterOperation],
        *,
        write_to: str | Path | None = None,
        progress_bar: bool = True,
        overwrite: bool = False,
    ) -> list[PyramidsDataset | Path]:
        """Download, then run client-side raster operations on what came back.

        The operations run *after* the fetch, on the in-memory pyramids object,
        so they are invisible to the engine-eligibility gate: a raw EEDAI read
        stays eligible and the analysis happens locally. That is the whole
        reason this is a separate step rather than more `ee` shaping.

        The download is made with `materialize=True` regardless of the
        constructor's setting, because an analysis needs the object rather than
        the path.

        Args:
            ops: One `RasterOperation`, a chain, or a sequence of them.
            write_to: When given, a directory; each derived raster is written
                there as a GeoTIFF and its path returned instead of the object.
            progress_bar: Whether the download shows a progress bar.
            overwrite: Whether an existing output file may be replaced, when
                `write_to` is given.

        Returns:
            One derived `pyramids.dataset.Dataset` per downloaded raster, or one
            `pathlib.Path` each when `write_to` is given.

        Raises:
            ValueError: If `ops` is not a raster operation, a chain or a
                sequence of them, or if this backend's sink is a batch one -
                `export_via="drive"` / `"gcs"` / `"asset"` send the pixels to
                that destination instead, so there is nothing in memory to
                analyse.

        Examples:
            - Composite, fetch, and take NDVI of what arrived:
                ```python
                >>> from earthlens.gee import GEE  # doctest: +SKIP
                >>> from earthlens.gee.analysis import Ndvi  # doctest: +SKIP
                >>> gee = GEE(...)  # doctest: +SKIP
                >>> ndvi = gee.analyze(Ndvi(nir=0, red=1))  # doctest: +SKIP
                ```
        """
        from earthlens.gee.analysis import RasterAnalysis

        # Refuse here rather than letting `download` report it. Its message is
        # about `materialize=True` - a flag this method sets itself and the
        # caller never passed - so it reads as a complaint about the wrong
        # thing when it surfaces from `analyze`. Asked of the resolved sink,
        # not matched against the `export_via` vocabulary, for the same reason
        # the constructor resolves it: a newly registered sink must not need an
        # edit here.
        if self._sink.is_batch:
            raise ValueError(
                f"analyze needs the pixels locally, but this backend exports "
                f"via {self._sink.name!r}, which queues the work remotely and "
                "returns a destination. Construct the backend with "
                "export_via='url' to analyse, or pull the result from the "
                "destination and run the analysis over those rasters yourself."
            )
        analysis = RasterAnalysis.of(ops)
        # materialize=True makes download return a DownloadResult (not the path
        # list the broad return type also admits), which run_on_result requires.
        result = cast(
            "DownloadResult",
            self.download(progress_bar=progress_bar, materialize=True),
        )
        return analysis.run_on_result(result, write_to=write_to, overwrite=overwrite)

    def _materialise(self, written: list[Any]) -> DownloadResult:
        """Read each written raster back as an in-memory pyramids object.

        Deliberately a read-back rather than handing over the `Dataset` the
        EEDAI path already built. That path closes the dataset, drops the last
        reference and renames the file in a carefully ordered `finally` — on
        Windows a lingering GDAL handle keeps the file locked and the rename
        fails. Returning the live object means not closing it, which would have
        to reorder that block; the round-trip costs one re-open per bucket and
        touches none of it. Handing back the live object is a worthwhile
        follow-up, not something to bolt on here.

        Args:
            written: What the sinks returned — paths, destinations or tasks.

        Returns:
            A `DownloadResult` carrying one artifact
            per bucket, with `obj` set for every entry that wrote a file.

        Raises:
            ValueError: If no entry wrote a file, which is every batch sink:
                there is nothing local to read back, so asking to materialise a
                queued export is a mistake worth naming.
        """
        # The batch sinks are refused at construction, so anything reaching
        # here with no written file produced no buckets at all - an empty result
        # is the honest answer, not an error about the sink.
        artifacts: list[Artifact] = []
        for entry in written:
            if isinstance(entry, Path):
                artifacts.append(
                    Artifact(
                        kind=self.OUTPUT_KIND,
                        path=entry,
                        obj=PyramidsDataset.read_file(entry),
                    )
                )
            elif isinstance(entry, str):
                artifacts.append(Artifact(kind=self.OUTPUT_KIND, destination=entry))
            else:
                artifacts.append(Artifact(kind=self.OUTPUT_KIND, task=entry))
        return DownloadResult(artifacts=tuple(artifacts))

    def _download_dataset(
        self, asset_id: str, bands: list[str], progress_bar: bool = True
    ) -> list[Path | str | TaskInfo | GeoDataFrame]:
        """Download one dataset's requested bands across the time buckets.

        Validates `asset_id` and every band against the catalog, clamps
        the request window to the dataset's published extent, builds the
        filtered collection, composites it per time bucket, and writes
        each bucket via `_api`.

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
                or if a write fails the size guard (see `_api`).
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
        if self._sampler is not None:
            return [self._sample_bucket(unit, var_info, bands) for unit in iterator]
        return [
            self._api(
                unit.payload,
                var_info,
                bands,
                unit.when,
                unit.bucket_start,
                unit.bucket_end,
            )
            for unit in iterator
        ]

    def _sample_bucket(
        self, unit: RasterWorkUnit, var_info: Dataset, bands: list[str]
    ) -> GeoDataFrame | TaskInfo:
        """Sample one composited bucket into a vector/tabular result.

        Routes the sampler's `ee.FeatureCollection` the way the sink does: a
        synchronous (`"url"`) sink pulls it into an in-memory `GeoDataFrame`
        through the `FeatureDownloader`, while a batch sink queues it as a
        table export via `TableSink` and returns the `TaskInfo`.

        Args:
            unit: The composited bucket, whose `payload` is the `ee.Image`.
            var_info: The catalog entry, for the output name.
            bands: The requested band ids, for the output name.

        Returns:
            A `GeoDataFrame` for a synchronous sink, or a `TaskInfo` for a
            queued table export.
        """
        from earthlens.gee.table_sink import TableSink
        from earthlens.gee.vector.download import FeatureDownloader

        sampler = cast("Sampler", self._sampler)
        prefix = f"{slug_asset_id(var_info.id)}_{'-'.join(bands)}_{unit.when:%Y%m%d}"
        work = FeatureWorkUnit(
            sampler.sample(unit.payload, self._geometry),
            unit.when,
            unit.bucket_start,
            unit.bucket_end,
        )
        if self._sink.is_batch:
            file_format = "GeoJSON" if self.OUTPUT_KIND == "vector" else "CSV"
            result: GeoDataFrame | TaskInfo = TableSink(
                destination=cast('Literal["drive", "gcs", "asset"]', self.export_via),
                folder=self.drive_folder,
                bucket=self.gcs_bucket,
                asset_id=self.asset_id,
                file_format=file_format,
                wait_for_export=self.wait_for_export,
            ).export(work, description=prefix)
        else:
            result = FeatureDownloader().to_gdf(work.features)
        return result

    def _build_collection(
        self, var_info: Dataset, bands: list[str], start: dt.datetime, end: dt.datetime
    ):
        """Build the filtered, cloud-masked, band-selected `ee.ImageCollection`.

        For an `ee_type="image"` dataset the single `ee.Image` is wrapped
        in a one-element collection so the rest of the pipeline is
        uniform. `filterDate` uses a half-open `[start, end]` window
        (Earth Engine convention); the `end` passed here is already
        bumped by one day by `_clamp_window_to_extent` so the
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
                `_build_collection`.
            var_info: The catalog entry (its `default_reducer`).
            start: Inclusive window start (clamped).
            end: Exclusive window end (clamped, already +1 day).

        Yields:
            `(timestamp, ee.Image, bucket_start, bucket_end)` tuples —
            `timestamp` is the bucket start (used in the filename), and
            `(bucket_start, bucket_end)` is the bucket's half-open date window
            (both `datetime.datetime`), which the EEDAI collection path
            re-composites client-side.
        """
        # Resolved once per request rather than per bucket: a parameterised
        # reducer (`percentile:90`) validates its argument at resolution time,
        # so a typo fails before any bucket is exported.
        reducer = resolve_reducer(self.reducer or var_info.default_reducer)
        if self.temporal_resolution == "raw" or not var_info.is_image_collection:
            yield RasterWorkUnit(reducer(collection), start, start, end)
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
            yield RasterWorkUnit(
                reducer(window),
                bucket_start.to_pydatetime(),
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

        For `export_via="url"`: size the request from the bbox and `scale` —
        or exactly, from `crs_transform`, when the grid is pinned; if either
        axis exceeds Earth Engine's
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

        The write itself is delegated to the resolved
        `Sink`, so this method decides only *whether
        the reader serves the bucket* and leaves *where the pixels go* to the
        sink.

        `engine="pixels"` replaces `getDownloadURL` on the `"url"` sink with
        `ee.data.computePixels` (`_export_via_pixels`). It evaluates the
        same composited `image` server-side, and Earth Engine applies the same
        32768-px per-axis cap to it, so the size check and `auto_split` apply
        there too - only the transport differs.

        A raw, no-compute request may instead be served by the pyramids-eo
        EEDAI reader — see `_try_via_eedai` for when, and
        `_export_via_eedai` for what that path does. The composited
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
            For `"url"`: the `pathlib.Path` of the written GeoTIFF.
            For `"drive"` / `"gcs"` / `"asset"`: a destination string
            (`"drive://<folder>/<prefix>"` / `"gs://<bucket>/<prefix>"` /
            `"ee://<asset_id>/<prefix>"`), or a
            `TaskInfo` when `wait_for_export=False`
            left the task running.

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
        # `is_batch`, not the name: the reader can serve any sink that wants
        # pixels now, so a synchronous sink added later gets the fast path
        # without an edit here - which is what makes the `Sink` seam a seam.
        if not self._sink.is_batch:
            served = self._try_via_eedai(
                var_info, bands, float(scale), prefix, bucket_start, bucket_end
            )
            if served is not None:
                return served
        self._warn_cog_ignored(var_info)
        self._warn_property_filter_ignored(var_info)
        # Only the Earth Engine paths need the `ee.Geometry`; the reader clips
        # to its own bbox / cutline.
        region = self._ee_region()
        # The resolved sink writes: it knows whether it pulls pixels now or
        # queues work, and - for the synchronous one - which of Earth Engine's
        # two pull APIs the resolved engine asked for. Adding a destination is
        # a `Sink` subclass, not another branch here.
        return self._sink.write(
            self,
            image,
            var_info=var_info,
            scale=float(scale),
            bands=bands,
            region=region,
            prefix=prefix,
        )

    def _try_via_eedai(
        self,
        var_info: Dataset,
        bands: list[str],
        scale: float,
        prefix: str,
        bucket_start: dt.datetime,
        bucket_end: dt.datetime,
    ) -> Path | None:
        """Serve one bucket from the EEDAI reader, or decline so Earth Engine does.

        Split out of `_api` so that method reads as the sink dispatch it
        is. Every question about routing to the reader is answered here: whether
        this request is the reader's at all, and what a refusal that arrives
        after routing has committed means.

        Args:
            var_info: The catalog entry being read.
            bands: The band ids the read asks for.
            scale: The resolved ground sample distance in metres.
            prefix: The filename stem for this bucket.
            bucket_start: Inclusive start of this bucket's date window.
            bucket_end: Exclusive end of that window.

        Returns:
            The written path when the reader served the bucket, else `None` —
            because no engine accepted the request, because the engine that
            accepted was not the reader, or because the reader refused after
            routing under an engine whose refusals are not fatal.

        Raises:
            ValueError: From `_use_eedai` when `engine="eedai"` was forced
                and the request cannot be served, and from the reader when a
                late refusal is fatal.
        """
        # An empty request is not one band: upstream opens every band the
        # asset has, so budget for that rather than under-counting.
        use_reader, plan = self._use_eedai(
            var_info,
            max(len(bands) or len(var_info.bands), 1),
            bucket_start,
            bucket_end,
        )
        # `plan.engine`, not just the boolean: an accepted plan says WHICH
        # engine accepted it, and only the reader's plans belong on the reader
        # path. Taking any yes here is what let a `pixels` plan - a request
        # carrying a cloud_mask or filters - be served from stored pixels with
        # the computation silently dropped.
        if not (use_reader and plan is not None and plan.engine == EedaiEngine.name):
            return None
        try:
            return self._export_via_eedai(
                var_info, bands, scale, prefix, plan, bucket_start, bucket_end
            )
        except reader_refusal_types() as exc:
            # The reader can still refuse after routing has committed: a band
            # set spanning resolution groups is refused by the collection
            # reader although the single-image one handles it, and only upstream
            # knows that. Under a forced engine that refusal is the answer;
            # under `auto` this is a request Earth Engine can serve, so falling
            # back keeps the contract that `auto` routes rather than fails.
            if self._engine.refusal_is_fatal():
                raise
            logger.warning(
                f"the EEDAI reader could not serve {var_info.id} "
                f"({exc}); falling back to Earth Engine for this bucket."
            )
            return None

    def _export_via_pixels(
        self,
        image: Any,
        var_info: Dataset,
        scale: float,
        bands: Sequence[str],
        prefix: str,
    ) -> Path:
        """Fetch one bucket through `ee.data.computePixels` → `<prefix>.tif`.

        Earth Engine's documented extraction call for a *computed* image. Unlike
        `getDownloadURL` it returns the bytes directly, and unlike the
        EEDAI reader it evaluates the computation graph, so a `cloud_mask` or
        `filters` survives.

        It returns the GeoTIFF bytes directly rather than a URL, so there is no
        second HTTP hop to retry and no zip to unpack: the body goes to a staged
        file and is renamed on success, the same write-then-rename the other
        paths use so a failure cannot leave a truncated raster at the final
        name.

        Earth Engine applies its 32768-px per-axis cap to a `computePixels`
        grid too - measured, not assumed: a 39,479 x 12 px request is refused
        with `Pixel grid dimensions (39479x12) must be less than or equal to
        32768`. So an oversize request is tiled when `auto_split=True`, and
        refused here with that remedy named when it is not, rather than left to
        surface as a raw Earth Engine error.

        Args:
            image: The composited `ee.Image` for this bucket.
            var_info: The catalog entry being read.
            scale: Ground sample distance in metres.
            bands: The bands to fetch, in order.
            prefix: The filename stem for this bucket.

        Returns:
            The written path.

        Raises:
            ValueError: If the engine declines to size the request, which means
                the catalog row carries no resolution to size from; or if the
                grid exceeds Earth Engine's per-axis cap and `auto_split` is
                off.
        """
        engine = self._engine
        # Narrowed for the same reason the batch export narrows to `_BatchSink`:
        # the grid and the request body are this engine's, not the ABC's, and
        # the caller has already dispatched on the name.
        assert isinstance(engine, PixelsEngine)  # the caller guarantees this
        nodata = self._export_config.nodata
        if nodata is not None:
            # The only way a fill value can reach these pixels: the request
            # body has no field for one, and an Earth Engine composite returns
            # masked pixels as 0 in a GeoTIFF - measured, on a CHIRPS window
            # over the Arabian Sea, which came back 0.0 over water with no
            # no-data declared. Unmasking first puts the caller's value there
            # instead, and the write declares it.
            image = image.unmask(nodata)
        serves, plan = engine.plan(self, var_info, max(len(bands), 1))
        if not serves or plan is None:
            raise ValueError(
                f"engine='pixels' cannot size a read of {var_info.id}; use "
                "engine='auto' so Earth Engine serves it instead."
            )
        grid = engine.grid_for(self, scale)
        if grid.longest_axis > EE_MAX_DIMENSION:
            if self.auto_split:
                return self._pixels_split_and_fetch(
                    image, var_info, scale, bands, prefix
                )
            _fits, reason = grid.fits(
                max_pixels=_EE_URL_MAX_PIXELS, max_dimension=EE_MAX_DIMENSION
            )
            raise ValueError(
                f"{var_info.id}: engine='pixels' would request "
                f"{grid.cols}x{grid.rows} px"
                f"{f' - {reason}' if reason else ''}. Earth Engine applies the "
                "same per-axis cap to computePixels as to getDownloadURL. Use a "
                "coarser scale, a smaller bbox, export_via='drive', or "
                "auto_split=True."
            )
        return self._fetch_one_pixels_tile(image, var_info, grid, bands, prefix)

    def _tiling_scale(self, scale: float) -> float:
        """Return the metre scale the AOI has to be split on.

        `split_aoi_for_url` sizes its tiles at a metre scale, which is the
        right input only when the request is sized from `scale`. A pinned
        `crs_transform` sets the pixel independently, and it can be far finer -
        so splitting at `scale` asked for too few tiles and each one still went
        out over the per-axis cap. Measured before the fix: a 4-degree AOI
        pinned at 0.0001 degrees is 40,000 px wide, while at `scale=5000` it
        looks 78 px wide, so `auto_split=True` produced a single tile 40,000 px
        across.

        The pinned pixel is therefore expressed back as the scale that would
        produce the same grid, which is what the splitter can take. The finer of
        the two axes decides, so the split errs toward more tiles rather than
        fewer.

        Args:
            scale: The resolved ground sample distance in metres.

        Returns:
            `scale` itself for an unpinned request, else the smaller scale that
            represents the pinned grid.
        """
        pinned = self._pinned_grid()
        if pinned is None:
            return scale
        sized = OutputGrid.for_metre_grid(pinned.bbox, crs=self.crs, scale=scale)
        factor = max(pinned.cols / sized.cols, pinned.rows / sized.rows)
        return scale / factor if factor > 1 else scale

    def _pixels_split_and_fetch(
        self,
        image: Any,
        var_info: Dataset,
        scale: float,
        bands: Sequence[str],
        prefix: str,
    ) -> Path:
        """Tile an oversize AOI, fetch each tile through `computePixels`, mosaic.

        The pixel API is capped per axis exactly as `getDownloadURL` is, so
        `auto_split=True` means the same thing here: split the AOI with
        `split_aoi_for_url`, fetch each sub-window, and merge the tiles.
        Each tile is sized through the engine, so a pinned grid stays pinned and
        the tiles keep sharing pixel edges.

        Args:
            image: The composited `ee.Image`.
            var_info: The catalog entry being read.
            scale: Ground sample distance in metres.
            bands: The bands to fetch, in order.
            prefix: The filename stem for the mosaic.

        Returns:
            The mosaicked path.
        """
        engine = self._engine
        assert isinstance(engine, PixelsEngine)  # the caller guarantees this
        sub_extents = split_aoi_for_url(self.space, self._tiling_scale(scale))
        logger.info(
            f"{var_info.id}: AOI exceeds the {EE_MAX_DIMENSION}-px per-axis cap "
            f"at scale={scale} m; fetching {len(sub_extents)} computePixels "
            "tile(s)."
        )
        tile_paths: list[Path] = []
        for index, sub in enumerate(sub_extents):
            tile_bbox = bbox_to_crs(
                (sub.west, sub.south, sub.east, sub.north), self.crs
            )
            tile_grid = engine.grid_for_window(self, tile_bbox, scale)
            # Checked per tile, not inferred from the split: the splitter works
            # in lat/lon degrees at a metre scale while the tile is sized in the
            # output CRS, so this is the only place the cap can be *known* to
            # hold. Raising here beats letting Earth Engine refuse the tile.
            if tile_grid.longest_axis > EE_MAX_DIMENSION:
                raise ValueError(
                    f"{var_info.id}: splitting the AOI left a tile of "
                    f"{tile_grid.cols}x{tile_grid.rows} px, still over Earth "
                    f"Engine's {EE_MAX_DIMENSION}-px per-axis cap. Use a "
                    "coarser scale or crs_transform, a smaller bbox, or "
                    "export_via='drive'."
                )
            tile_prefix = f"{prefix}_tile_{index:04d}"
            tile_paths.append(
                self._fetch_one_pixels_tile(
                    image, var_info, tile_grid, bands, tile_prefix, as_cog=False
                )
            )
        # The tiles are plain GeoTIFFs whatever `cog` says - converting each one
        # only to merge them would be wasted work - and the mosaic carries the
        # flag instead.
        return self._mosaic_tiles(tile_paths, prefix, as_cog=self.cog)

    def _fetch_one_pixels_tile(
        self,
        image: Any,
        var_info: Dataset,
        grid: OutputGrid,
        bands: Sequence[str],
        prefix: str,
        *,
        as_cog: bool | None = None,
    ) -> Path:
        """Fetch one `computePixels` window to `<prefix>.tif`.

        The single-window worker shared by the whole-AOI path and the tiled one,
        so the staging, the COG choice and the handle release are written once.

        Args:
            image: The composited `ee.Image`.
            var_info: The catalog entry, for the log line.
            grid: The window to request.
            bands: The bands to fetch, in order.
            prefix: The filename stem.

        Returns:
            The written path.
        """
        engine = self._engine
        assert isinstance(engine, PixelsEngine)  # only this engine gets here
        body = engine.request(self, image, grid, list(bands))
        logger.info(
            f"computePixels {var_info.id} [{', '.join(bands) or 'all bands'}] "
            f"at {grid.cols}x{grid.rows} px in {grid.crs}"
        )
        target = self.root_dir / f"{prefix}.tif"
        staged = self.root_dir / f"{prefix}.partial.tif"
        write_cog = self.cog if as_cog is None else as_cog
        nodata = self._export_config.nodata
        try:
            payload = ee.data.computePixels(body)
            staged.write_bytes(payload)
            reader = PyramidsDataset.read_file(staged)
            try:
                writer = reader if nodata is None else _declaring(reader, nodata)
                try:
                    if write_cog:
                        writer.cog.to_cog(str(target))
                    else:
                        writer.to_file(str(target))
                finally:
                    if writer is not reader:
                        close_quietly(writer)
            finally:
                close_quietly(reader)
        finally:
            _discard_quietly(staged)
        logger.info(f"Wrote {target} (computePixels{', COG' if write_cog else ''})")
        return target

    def _mosaic_tiles(
        self, tile_paths: list[Path], prefix: str, *, as_cog: bool = False
    ) -> Path:
        """Merge per-tile rasters into `<prefix>.tif` and delete the tiles.

        Shared by both tiled paths, which differ only in how a tile is fetched
        and in whether the result is a COG. `merge_rasters` writes a plain
        GeoTIFF, so a mosaic that has to be cloud-optimised is merged to a
        staged file and rewritten through `Dataset.cog` - otherwise `cog=True`
        would be honoured for every tile and then lost in the stitch.

        `as_cog` is passed rather than read from `self.cog` so each path keeps
        its own contract: the pixel API writes its own raster and honours the
        flag, while `getDownloadURL` documents that it ignores it, and a tiled
        request must not quietly differ from the single-request one.

        Args:
            tile_paths: The written tiles, in order.
            prefix: The filename stem for the mosaic.
            as_cog: Write the mosaic as a Cloud Optimized GeoTIFF.

        Returns:
            The mosaicked path.
        """
        target = self.root_dir / f"{prefix}.tif"
        # Carry the first tile's own no-data marker onto the mosaic
        # explicitly -- None when it declares none, asking for no marker at
        # all rather than a stray literal like the string "none".
        first_tile = PyramidsDataset.read_file(tile_paths[0])
        try:
            tile_no_data = first_tile.no_data_value
        finally:
            # Release before the unlink below: a live handle holds a Windows
            # lock, and `Path.unlink` would raise PermissionError on it.
            first_tile.close()
        fill = (
            tile_no_data[0] if isinstance(tile_no_data, (list, tuple)) else tile_no_data
        )
        merged = self.root_dir / f"{prefix}.merged.tif" if as_cog else target
        merge_rasters(
            [str(path) for path in tile_paths], str(merged), no_data_value=fill
        )
        if as_cog:
            try:
                reader = PyramidsDataset.read_file(merged)
                try:
                    reader.cog.to_cog(str(target))
                finally:
                    # Release before the unlink: a live handle holds a Windows
                    # lock, the same reason the no-data probe above closes.
                    close_quietly(reader)
            finally:
                _discard_quietly(merged)
        for path in tile_paths:
            path.unlink(missing_ok=True)
        logger.info(
            f"Stitched {len(tile_paths)} tile(s) into {target} via pyramids"
            f"{', COG' if as_cog else ''}."
        )
        return target

    def _export_via_url(
        self, image, var_info: Dataset, scale: float, region, prefix: str
    ) -> Path:
        """Fetch a GeoTIFF from `image.getDownloadURL`; enforce the 32768-px cap.

        Earth Engine returns a single GeoTIFF when one band is exported and
        a zip archive of per-band GeoTIFFs when several are. Both shapes
        are routed through pyramids: single tifs via `Dataset.from_bytes`
        (writes the in-memory body to a `/vsimem/` path then materialises it
        on disk), zips via `Dataset.from_archive` (chained `/vsizip/`,
        merging members into one multi-band tif).

        Oversized AOIs (either axis above `EE_MAX_DIMENSION` px at
        `scale`) take one of two paths: when `auto_split=True` was passed
        to the constructor, the bbox is tiled, each tile is downloaded
        individually, and the tiles are mosaicked into one GeoTIFF via
        `pyramids.dataset.merge.merge_rasters`; otherwise a
        `ValueError` is raised with a coarser-scale / smaller-bbox /
        `export_via="drive"` hint.

        A pinned `crs_transform` changes both halves of that: the request sends
        the transform in place of `scale`, which Earth Engine treats as an
        either/or, and the cap is checked against the grid the transform
        implies rather than against an estimate made at `scale`.
        """
        # Two sizings, deliberately. `estimate_pixel_dims` is pyramids' documented
        # worst-case UPPER bound, which is the right thing to make the decision
        # on: it over-counts, so it can only split or refuse too eagerly, never
        # too late. `OutputGrid.for_earth_engine` predicts the grid Earth Engine
        # will actually build, which is the right thing to put in the message -
        # quoting the upper bound told the caller a pixel count Earth Engine
        # never produces. The decision still rests on the bound, so this changes
        # no behaviour, only what the error says.
        # A pinned grid is the exception to the two sizings above: its pixel
        # size comes from the transform, not from `scale`, so an estimate made
        # at `scale` describes a grid this request will not produce. The pinned
        # grid is exact, so it answers both the decision and the message.
        pinned = self._pinned_grid()
        width_px, height_px = (
            (pinned.cols, pinned.rows)
            if pinned is not None
            else self.space.estimate_pixel_dims(scale)
        )
        if max(width_px, height_px) > EE_MAX_DIMENSION:
            if self.auto_split:
                return self._auto_split_and_download(image, var_info, scale, prefix)
            grid = pinned or OutputGrid.for_earth_engine(
                (
                    self.space.longitude_min,
                    self.space.latitude_min,
                    self.space.longitude_max,
                    self.space.latitude_max,
                ),
                crs=self.crs,
                scale=scale,
            )
            _fits, reason = grid.fits(
                max_pixels=_EE_URL_MAX_PIXELS, max_dimension=EE_MAX_DIMENSION
            )
            # A pinned request was not sized by `scale`, so quoting the scale
            # would describe a grid it never asked for.
            sized = (
                f"pinned by crs_transform={list(pinned.crs_transform or ())}"
                if pinned is not None
                else f"at scale={scale} m"
            )
            raise ValueError(
                f"{var_info.id}: the requested AOI {sized} is "
                f"{grid.cols}x{grid.rows} px on Earth Engine's grid"
                f"{f' - {reason}' if reason else ''}. Use a coarser scale, a "
                "smaller bbox, export_via='drive', or auto_split=True."
            )
        return self._download_one_url_tile(image, region, scale, prefix)

    def _reducer_name(self, var_info: Dataset) -> str:
        """Return the requested reducer as a name, whatever form it arrived in.

        `reducer=` accepts a name, a `percentile:<q>` spec or a `Reducer`
        instance, and two places need the *name*: the guard that refuses the
        reader a composite it would compute differently, and the reader's own
        `reducer=` keyword, which takes a string from its own vocabulary.

        Args:
            var_info: The catalog entry, for its `default_reducer`.

        Returns:
            The reducer's registry name.
        """
        requested = self.reducer or var_info.default_reducer
        if isinstance(requested, str):
            return requested.split(":", 1)[0]
        return type(requested).name

    @property
    def _engine_config(self) -> EngineConfig:
        """Return the engine settings as one value object, built from the live ones.

        Derived rather than snapshotted in `__init__`: `crs`, `scale`,
        `cloud_mask` and the rest stay public and writable, so a stored copy
        would silently go stale the moment one was reassigned - which is why
        nothing read the stored one.

        Returns:
            The current engine configuration.
        """
        return EngineConfig(
            # The resolved name, not the raw attribute: `engine=` accepts an
            # `Engine` instance, and this field is a `str`, so passing the
            # attribute through made a perfectly good instance fail validation
            # inside a `ValueError` handler that reported it as a sizing
            # failure.
            engine=self._engine.name,
            reducer=self.reducer,
            crs=self.crs,
            scale=self.scale,
            cloud_mask=self.cloud_mask,
            filters=tuple(self.filters),
            property_filter=self.property_filter,
            region=self.region,
            discover_extent=bool(self.discover_extent),
            high_volume=bool(self.high_volume),
            crs_transform=self.crs_transform,
        )

    def _pinned_grid(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        cutline: Any = None,
    ) -> OutputGrid | None:
        """Return the grid a `crs_transform` pins, or `None` when none was given.

        One builder for every path that needs it, so a pinned grid cannot be
        derived two different ways: the pixel engine asks for it to size its
        request, the `getDownloadURL` path asks for it to decide whether the
        request fits Earth Engine's per-axis cap, and a tiled request asks for
        it per tile - which is what keeps the tiles on the same lattice.

        Args:
            bbox: The window in the output CRS, or `None` for the whole AOI.
            cutline: The region to clip to, when a window was given; ignored for
                the whole AOI, which carries its own.

        Returns:
            The pinned `OutputGrid` over that window,
            or `None` when the grid is sized from `scale`.

        Raises:
            ValueError: If the AOI reprojects to non-finite bounds in the output
                CRS. The transform itself was validated at construction.
        """
        crs_transform = self._engine_config.crs_transform
        if crs_transform is None:
            return None
        if bbox is None:
            bbox, cutline = self._eedai_window()
        return OutputGrid.from_crs_transform(
            bbox, crs=self.crs, crs_transform=crs_transform, cutline=cutline
        )

    def _eedai_eligible(self, var_info: Dataset) -> bool:
        """Return whether this request is a raw read the EEDAI reader can serve.

        The pyramids-eo reader materialises pixels from a real asset id; it
        cannot execute an Earth Engine computation graph. So it can only stand
        in for `getDownloadURL` when nothing server-side shapes the image: no
        per-image `cloud_mask` and no collection `filters`. The asynchronous
        sinks are Earth Engine-only.

        A single `ee_type="image"` asset is read directly; an
        `ee_type="image_collection"` is composited client-side by the reader
        (a reducer over a date window), which is why a `cloud_mask` or
        `filters` — server-side shaping — still disqualifies either.

        The output CRS may be `"EPSG:4326"` or a metre-based projected CRS.
        The reader reads `bbox` in the *target* CRS, so this backend reprojects
        its lat/lon AOI into that CRS first (see `_eedai_window`); a
        geographic CRS other than EPSG:4326, or a projected one whose axis is
        not in metres, is not sized correctly by the metre `scale` and stays on
        Earth Engine. A pinned `crs_transform` is declined for the same kind of
        reason: the reader sizes its own grid from `scale` and takes no
        transform, so it could not land on the lattice that was pinned.

        Args:
            var_info: The catalog entry for the dataset being fetched.

        Returns:
            `True` when the request is a raw, no-compute read on an unpinned
            grid.
        """
        return (
            self.export_via == "url"
            # The config owns the "is anything shaping this server-side?"
            # question, so the reader's eligibility and the engines read one
            # definition instead of each repeating the two-term test.
            and not self._engine_config.needs_server_side_compute
            and var_info.ee_type in ("image", "image_collection")
            and self._eedai_crs_supported()
            # The reader sizes its own metre grid from `scale` and takes no
            # transform, so it cannot honour a pinned one. Routing a pinned
            # request to it would return pixels on a grid other than the one
            # the caller pinned, which is worse than not using the reader.
            and self._engine_config.crs_transform is None
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
        from pyproj import CRS
        from pyproj.exceptions import CRSError

        try:
            crs = CRS.from_user_input(self.crs)
        except CRSError:
            # Only an unparseable CRS is "unsupported". An import failure or any
            # other error is a broken environment, which must surface rather
            # than be reported as a CRS this backend cannot serve.
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
        return bbox_to_crs(latlon_bbox, self.crs)

    def _eedai_output_grid(
        self, bbox: tuple[float, float, float, float], scale: float
    ) -> tuple[int, int]:
        """Size a pixel grid for `bbox` (already in the output CRS) at `scale`.

        Dispatches on the output CRS: EPSG:4326 keeps the geographic grid with
        its `cos(latitude)` longitude shortening (`_eedai_grid`); a
        metre-based projected CRS sizes each axis by its span over the metre
        `scale` directly.

        Args:
            bbox: The window `(min_x, min_y, max_x, max_y)` in `self.crs`.
            scale: Target ground sample distance in metres.

        Returns:
            `(rows, cols)`, at least one pixel per axis.
        """
        return OutputGrid.for_metre_grid(bbox, crs=self.crs, scale=scale).as_rows_cols()

    @staticmethod
    def _reader_end(bucket_end: dt.datetime, bucket_start: dt.datetime) -> str:
        """Convert an exclusive bucket end to the inclusive date the reader wants.

        This backend's buckets are half-open, matching Earth Engine's
        `filterDate`, whose `end` is exclusive. The reader's `end` is
        *inclusive*: a bare date becomes `startTime < <end + 1 day>`. Passing
        the exclusive boundary straight through would therefore read one extra
        day per bucket and make consecutive buckets overlap.

        Args:
            bucket_end: The bucket's exclusive end.
            bucket_start: The bucket's inclusive start, used to clamp a bucket
                shorter than a day rather than invert its window.

        Returns:
            The `YYYY-MM-DD` date the reader should treat as inclusive, so its
            window covers exactly the same instants as `filterDate`.
        """
        # A bucket shorter than a day — a time-bearing `fmt` can leave one at
        # the end of a range — would otherwise produce an end before its start,
        # spending a discovery round-trip on a window that cannot match.
        return max(bucket_end - dt.timedelta(days=1), bucket_start).strftime("%Y-%m-%d")

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
        # A region's bounds are the AOI, and they can be in any CRS, so that
        # case is resolved here; a plain lat/lon bbox is already the answer and
        # `OutputGrid.latlon_bbox` is what says so for a grid in another CRS.
        region = self.region
        if region is not None:
            latlon = region
            crs = getattr(region, "crs", None)
            if crs is not None and not self._same_crs(crs, _EEDAI_NATIVE_CRS):
                latlon = self._region_cache.get(_EEDAI_NATIVE_CRS)
                if latlon is None:
                    latlon = region.to_crs(_EEDAI_NATIVE_CRS)
                    self._region_cache[_EEDAI_NATIVE_CRS] = latlon
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
        than `_EEDAI_MAX_SCENES`, or whose scenes together exceed the
        single-pass pixel budget, is declined so Earth Engine's server-side
        reduce serves it instead.

        Args:
            var_info: The collection's catalog entry.
            band_count: Bands requested; the reader holds every band per scene.
            bucket_start: Inclusive start of the bucket's date window.
            bucket_end: Exclusive end of the bucket's date window.

        Returns:
            An `EedaiPlan`; a collection is served in one pass
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
        # Resolve before comparing: `reducer=` accepts a `Reducer` instance as
        # well as a name, and an instance is never a member of a set of strings,
        # so testing the raw value let `Mosaic()` walk straight past this guard.
        reducer = self._reducer_name(var_info)
        if reducer not in _COLLECTION_REDUCERS:
            # The reader takes a name from its own vocabulary. Anything outside
            # it - `percentile` is the first such reducer - would reach the
            # reader verbatim and raise a `ValueError` that is deliberately not
            # in `_reader_errors`, aborting the download after the scenes had
            # already been fetched. Earth Engine can express all of them.
            return EnginePlan.declined(
                f"reducer={reducer!r} is not in the reader's vocabulary, so "
                "Earth Engine composites this instead"
            )
        if reducer in _EEDAI_UNSUPPORTED_REDUCERS:
            # Checked before the network call: no point discovering scenes for a
            # composite the reader cannot reproduce.
            return EnginePlan.declined(
                f"reducer={reducer!r} does not mean the same thing client-side: Earth Engine's is last-wins, while the reader returns the first scene of the stack, so Earth Engine composites this instead"
            )
        # The adapter, not the raw module: it owns the lock and the identity
        # check around a reader call, which is the whole reason it exists.
        # Built outside the discovery `try`, so a credential problem is never
        # reported as "no scenes". Under a forced engine it raises; under
        # `auto` it warns and falls back, because `auto`'s contract is to route
        # a request Earth Engine can still serve rather than to fail it - but
        # loudly, so a fixable key does not silently disable the fast path.
        try:
            # Called for its failure, not its value: the adapter binds the
            # credential for the read itself, but building it here keeps a
            # broken key from being reported later as "no scenes".
            self._eedai_credentials()
        except AuthenticationError:
            if self.engine == "eedai":
                raise
            logger.warning(
                f"the EEDAI credential could not be built, so {var_info.id} "
                "falls back to Earth Engine; fix the service key to use the "
                "fast path."
            )
            return EnginePlan.declined('the EEDAI credential could not be built')
        window_start = bucket_start.strftime("%Y-%m-%d")
        window_end = self._reader_end(bucket_end, bucket_start)
        # One catalog query per bucket, by necessity: every bucket has a
        # distinct window, and `ReadCost` reports only aggregates, so a single
        # whole-run discovery cannot be split back into per-bucket counts.
        # Caching keyed on the window was measured to never hit for this reason
        # and was removed rather than left in as dead weight. Reducing this to
        # one query per run needs upstream to expose the per-scene times.
        try:
            cost = self._eedai_reader().estimate_cost(
                var_info.id,
                start=window_start,
                end=window_end,
                bbox=self._eedai_latlon_aoi(),
                # The AOI handed over is lat/lon, so it must be labelled as
                # such: upstream reads `bbox` *in* `crs`, and passing the
                # output CRS here would have degrees read as projected metres
                # and discover scenes over the wrong ground.
                crs=_EEDAI_NATIVE_CRS,
                property_filter=self.property_filter,
            )
        except _reader_errors(self._eedai_reader().module) as exc:
            # A discovery failure is a fallback, not a crash - but it is
            # worth more than an info line, because a persistent one
            # silently disables the fast path for the whole run.
            logger.warning(
                f"EEDA scene discovery failed for {var_info.id}: {exc}. This "
                "bucket falls back to Earth Engine."
            )
            return EnginePlan.declined(
                f'scene discovery for {var_info.id} failed ({exc})'
            )
        if not cost.scene_count:
            # A property of the data, not of the request: this window and AOI
            # simply hold no scenes. Marked so a forced engine skips the bucket
            # rather than aborting a run whose earlier buckets wrote fine —
            # "raises if the request is not eligible" is about the request, and
            # this one is.
            return EnginePlan.declined(
                f"{_EEDAI_NO_SCENES}: no {var_info.id} scenes in this bucket's window and AOI"
            )
        if cost.scene_count > _EEDAI_MAX_SCENES:
            return EnginePlan.declined(
                f'{cost.scene_count:,} scenes in this bucket, over the {_EEDAI_MAX_SCENES:,}-scene cap — Earth Engine reduces this server-side instead of the reader fetching every scene'
            )
        # `estimate_earthengine_cost` is authoritative for the *scene count*;
        # the per-scene native footprint is sized from the catalog's metre
        # `spatial_resolution`, since EEDA reports `min_pixel_size` in the
        # asset's own CRS units (degrees for a geographic asset), which the
        # metre-based grid would misread by ~1e5x.
        native_scale = var_info.spatial_resolution
        if not native_scale:
            return EnginePlan.declined(
                f'{var_info.id} has no native resolution to size the read'
            )
        bbox, _cutline = self._eedai_window()
        # One scene's window must satisfy the same budgets a single-image read
        # does - the per-axis cap is about the window's *shape*, which no
        # scene-count multiple would catch - so the shared gate runs first.
        fits, reason = self._eedai_native_fits(var_info, bbox, band_count)
        if not fits:
            return EnginePlan.declined(reason)
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
            return EnginePlan.declined(
                f'about {total:,} px across {cost.scene_count:,} scenes on a {cols}x{rows} grid, over the {_EEDAI_MAX_PIXELS:,}-px single-pass budget'
            )
        return EnginePlan.serves(tiles=cost.scene_count)

    def _eedai_verdict(
        self,
        var_info: Dataset,
        band_count: int,
        bucket_start: dt.datetime | None,
        bucket_end: dt.datetime | None,
    ) -> EedaiPlan:
        """Build the serve/decline verdict, dispatching on the asset kind.

        A single image is sized by `_eedai_single_image_plan`; an image collection is
        sized by `_eedai_collection_fits`, which counts the scenes the
        reader would fetch and reduce.

        Args:
            var_info: The catalog entry.
            band_count: Bands requested.
            bucket_start: Inclusive start of the bucket window (collections).
            bucket_end: Exclusive end of the bucket window (collections).

        Returns:
            The `EedaiPlan` verdict.
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
          `_EEDAI_MAX_TILING_RATIO`), where Earth Engine's server-side
          aggregation returns a small raster instead of fetching `ratio**2`
          native pixels per output pixel;
        * the whole read would still fetch more than
          `_EEDAI_MAX_NATIVE_PIXELS` — the tile budget bounds memory,
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
            An `EedaiPlan`. `tile_size` is `None` for a single read, and
            `reason` explains a `False` for the fallback log line or the
            forced-engine error.
        """
        bbox, cutline = self._eedai_window()
        fits, reason = self._eedai_native_fits(var_info, bbox, band_count)
        if fits:
            return EnginePlan.serves(tiles=1)
        native_scale = var_info.spatial_resolution
        if not native_scale:
            return EnginePlan.declined(reason)
        if cutline is not None:
            return EnginePlan.declined(
                f'{reason}, and it cannot be tiled behind a cutline'
            )
        if self.resample != _EEDAI_TILING_RESAMPLE:
            return EnginePlan.declined(
                f'{reason}, and it cannot be tiled with resample={self.resample!r} — an interpolating resampler would differ from the un-tiled read at the tile seams'
            )
        scale_m = float(self.scale or native_scale)
        native_ratio = max(scale_m / float(native_scale), 1.0)
        if native_ratio > _EEDAI_MAX_TILING_RATIO:
            return EnginePlan.declined(
                f'{reason}, and tiling it would be worse than Earth Engine: at scale={scale_m:g} m over a {native_scale:g} m asset the reader fetches about {native_ratio**2:,.0f} native px per output px, which Earth Engine aggregates server-side instead'
            )
        native_rows, native_cols = self._eedai_output_grid(bbox, float(native_scale))
        native_total = native_rows * native_cols * max(band_count, 1)
        if native_total > _EEDAI_MAX_NATIVE_PIXELS:
            return EnginePlan.declined(
                f"{reason}, and tiling it would still fetch about {native_total:,} native px, over the {_EEDAI_MAX_NATIVE_PIXELS:,}-px ceiling on one read's total work"
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
            return EnginePlan.declined(
                f"{reason}, and no tile is small enough: the reader's {_EEDAI_WINDOW_PAD}-px window padding already exceeds the per-tile budget here"
            )
        rows, cols = self._eedai_output_grid(bbox, scale_m)
        tiles = math.ceil(rows / tile_size) * math.ceil(cols / tile_size)
        if tiles > _EEDAI_MAX_TILES:
            return EnginePlan.declined(
                f'{reason}, and tiling it would take {tiles:,} tiles (over the {_EEDAI_MAX_TILES:,}-tile ceiling); every tile is its own fetch and they are opened together to mosaic'
            )
        return EnginePlan.serves(tiles=tiles, tile_size=tile_size)

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
                either ineligible — it needs server-side compute (a
                `cloud_mask` or `filters`) or targets a CRS the reader cannot
                size (not EPSG:4326 or a metre-based projected CRS) — or
                eligible but declined by `_eedai_verdict`, which the
                message names. For a single image: no native resolution, a
                polygon cutline, a non-nearest `resample`, or tiling costing
                more than Earth Engine would. For a collection: an unsupported
                reducer, more scenes than the cap, or a stack over the
                single-pass budget. A bucket that merely holds no scenes is
                skipped rather than raised, since that is a property of the
                data and not of the request.
        """
        return self._engine.plan(self, var_info, band_count, bucket_start, bucket_end)

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
            EPSG:4326. The result is memoised per target CRS, since `region`
            never changes for the life of the backend and a many-bucket run
            would otherwise warp the same geometry once per bucket.
        """
        if region is None:
            return None
        target = self.crs
        cached = self._region_cache.get(target)
        if cached is not None:
            return cached
        crs = getattr(region, "crs", None)
        if crs is None:
            if target.upper() == _EEDAI_NATIVE_CRS:
                self._region_cache[target] = region
                return region
            set_crs = getattr(region, "set_crs", None)
            if callable(set_crs):
                region = set_crs(_EEDAI_NATIVE_CRS)
            assumed = region.to_crs(target)
            self._region_cache[target] = assumed
            return assumed
        if self._same_crs(crs, target):
            self._region_cache[target] = region
            return region
        reprojected = region.to_crs(target)
        self._region_cache[target] = reprojected
        return reprojected

    @staticmethod
    def _same_crs(region_crs: Any, target: str) -> bool:
        """Return whether a region's CRS already is the output CRS.

        Compared as CRS objects rather than by string-parsing an `AUTH:CODE`
        tail. Parsing the code answers the wrong question twice: it drops the
        authority, so any non-EPSG code collides with the EPSG code of the same
        number, and it has nothing to read at all when the target is a PROJ
        string or WKT — under which an already-correct region gets warped
        needlessly. Comparing objects lets PROJ decide, which also gets the
        genuinely-equal cases right: `ESRI:102100` and `EPSG:3857` describe one
        CRS and compare equal, where the codes never would.

        Args:
            region_crs: The region's own CRS object.
            target: The output CRS, in any form pyproj accepts.

        Returns:
            `True` when the two describe the same CRS; `False` when they differ
            or either cannot be parsed (in which case reprojecting is the safe
            answer).
        """
        return same_crs(region_crs, target)

    @staticmethod
    def _eedai_grid(
        bbox: tuple[float, float, float, float], scale: float
    ) -> tuple[int, int]:
        """Size a pixel grid for a lat/lon `bbox` at a metre `scale`.

        The reader sizes its output in the units of the output CRS (degrees
        here), so the metre `scale` has to become an explicit grid. This is
        deliberately not `SpatialExtent.estimate_pixel_dims`, which
        pyramids documents as a worst-case *upper bound* for cap pre-checks
        — it over-counts both axes (and unevenly, so a square AOI comes out
        non-square). Here the real span is used, with longitude degrees
        shortened by `cos(latitude)` at the AOI's equatorward edge - the
        latitude where a degree of longitude is longest, and so the one that
        needs the most pixels to hold the requested `scale`. Sizing on any
        poleward latitude instead would deliver pixels coarser than asked.

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
              pixels, because a degree of longitude is shorter at that
              latitude:
                ```python
                >>> from earthlens.gee.backend import GEE
                >>> GEE._eedai_grid((31.2, 29.9, 31.3, 30.0), 90.0)
                (124, 108)

                ```
            - Sitting on the equator the two axes match, because a degree of
              longitude is not shortened there at all:
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
        return geographic_grid(bbox, scale)

    def _warn_cog_ignored(self, var_info: Dataset) -> None:
        """Say so, once, when `cog=True` cannot apply to this request.

        `cog=` reaches the paths that write the raster themselves - the EEDAI
        reader and the pixel API - and nothing else, so a `getDownloadURL` or
        batch request silently yields a plain GeoTIFF. Without a notice the only
        symptom is an output that is not a COG.

        It is therefore not enough to ask "did the reader decline?": the pixel
        engine reaches this method on the same branch and *does* honour the
        flag, so warning there would be a notice about nothing.

        Args:
            var_info: The catalog entry being written (named in the notice).
        """
        if not self.cog or self._cog_warned:
            return
        if self._engine.name == PixelsEngine.name and not self._sink.is_batch:
            return
        self._cog_warned = True
        logger.warning(
            f"cog=True has no effect for {var_info.id}: it applies to the paths "
            "that write the raster themselves (the EEDAI reader and "
            "engine='pixels'), and this request is served by Earth Engine's "
            "getDownloadURL or a batch export. A plain GeoTIFF is written "
            "instead."
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
        return self._eedai_reader().credential

    def _eedai_reader(self) -> EedaiReader:
        """Return the EEDAI adapter for this request, built once.

        Memoised per instance for the same reason the credential was: building
        it writes inline key material to a transient temp file, so rebuilding it
        per bucket would scatter those across a multi-band, multi-date download.

        Returns:
            The bound `EedaiReader`.

        Raises:
            AuthenticationError: If the credential cannot be built.
            ImportError: If `pyramids-eo` (the `[eedai]` extra) is missing.
        """
        if self._eedai_reader_adapter is None:
            _account, service_key, _project = self._resolve_credentials()
            self._eedai_reader_adapter = EedaiReader.from_service_key(service_key)
        return self._eedai_reader_adapter

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
        is not necessarily refused — `_eedai_single_image_plan` may still serve it by
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

        The reader sizes its output in the units of `crs` - degrees under
        EPSG:4326, the projection's metres otherwise - whereas `scale` here is
        always Earth Engine's metres. `_eedai_output_grid` reconciles the
        two by turning `scale`
        into an explicit `shape` over the same window
        `_eedai_window` hands the reader, so the grid and the read
        window always describe the same ground area.

        The raster is written as a plain GeoTIFF, or as a Cloud Optimized
        GeoTIFF (tiled, with overviews) when `cog=True` was passed to the
        constructor.

        Args:
            var_info: The catalog entry; its `id` is the Earth Engine asset.
            bands: Band ids to read.
            scale: Output pixel size in metres.
            prefix: Output filename stem (no extension).
            plan: The `EedaiPlan` verdict from `_eedai_verdict`,
                computed once by the caller so the routing decision and the
                read it performs cannot disagree.
            bucket_start: Inclusive start of the bucket's date window. Required
                for an `image_collection`, whose scenes the reader composites;
                ignored for a single image.
            bucket_end: Exclusive end of that window.

        Returns:
            The `pathlib.Path` of the written GeoTIFF.

        Raises:
            ImportError: If `pyramids-eo` (the `[eedai]` extra) is missing.
            AuthenticationError: If the reader's credentials cannot be built.
        """
        # The adapter owns the lock, the identity check and the credential, so
        # the process-global GDAL configuration a reader call mutates is only
        # ever touched from one place.
        reader = self._eedai_reader()
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
            # Caveat: the reader takes each band's nodata from the scene's own
            # dataset, and EEDAI declares none, so its statistical reducers run
            # unmasked and fold a scene's fill pixels into the result where
            # Earth Engine would mask them. The values agree wherever the scenes
            # carry no fill over the AOI. There is no caller-side remedy today -
            # the composite read takes no `nodata` argument - so closing this
            # needs the driver (or upstream) to declare the fill. Stated in the
            # constructor docstring and the usage guide rather than left here,
            # since it is a user-visible difference, not an implementation note.
            composite_kwargs = {
                "start": bucket_start.strftime("%Y-%m-%d"),
                "end": self._reader_end(bucket_end, bucket_start),
                # A name, not the resolved object: the reader's `reducer=`
                # takes one of its own vocabulary's strings.
                "reducer": self._reducer_name(var_info),
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
                window=reader.window_type(
                    bbox=bbox,
                    crs=self.crs,
                    shape=self._eedai_output_grid(bbox, scale),
                    resample=self.resample,
                ),
                geometry=cutline,
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

        A pinned `crs_transform` is sent instead of `scale` (Earth Engine takes
        one or the other). Every tile sends the same transform and its own
        region, which is what keeps the tiles on one lattice: the transform
        fixes the pixel size and origin, the region only says how far to go.
        """
        params: dict[str, Any] = {
            "crs": self.crs,
            "region": region,
            "format": "GEO_TIFF",
        }
        # Earth Engine documents `crs_transform` and `scale` as mutually
        # exclusive, so a pinned request sends the transform in place of the
        # scale rather than beside it.
        crs_transform = self._engine_config.crs_transform
        if crs_transform is None:
            params["scale"] = scale
        else:
            params["crs_transform"] = list(crs_transform)
        url = image.getDownloadURL(params)
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
        constructor and the full AOI exceeds `EE_MAX_DIMENSION` px
        per axis. The bbox is split with `split_aoi_for_url`, each
        sub-extent is downloaded via `_download_one_url_tile`, and
        the per-tile tifs are mosaicked into `<prefix>.tif` with
        `pyramids.dataset.merge.merge_rasters`. `no_data_value` is
        passed explicitly as the first tile's own marker, so a real value
        it declares carries over. Tiles that declare no no-data of their
        own — the usual case here, since the download path leaves it unset
        — pass Python `None` through, which asks for no marker at all
        rather than stamping one. Per-tile tifs are deleted on success.
        """
        sub_extents = split_aoi_for_url(self.space, self._tiling_scale(scale))
        logger.info(
            f"{var_info.id}: AOI exceeds {EE_MAX_DIMENSION}-px per-axis cap at "
            f"scale={scale} m; auto-splitting into {len(sub_extents)} tile(s)."
        )
        tile_paths: list[Path] = []
        for k, sub in enumerate(sub_extents):
            # A pinned request sends its transform per tile, so the tile is as
            # cap-bound as the whole AOI was; the unpinned case is guaranteed by
            # the splitter itself, which sized these extents at this scale.
            pinned_tile = self._pinned_grid(
                bbox=bbox_to_crs((sub.west, sub.south, sub.east, sub.north), self.crs)
            )
            if pinned_tile is not None and pinned_tile.longest_axis > EE_MAX_DIMENSION:
                raise ValueError(
                    f"{var_info.id}: splitting the AOI left a tile of "
                    f"{pinned_tile.cols}x{pinned_tile.rows} px, still over "
                    f"Earth Engine's {EE_MAX_DIMENSION}-px per-axis cap. Use a "
                    "coarser scale or crs_transform, a smaller bbox, or "
                    "export_via='drive'."
                )
            sub_region = ee.Geometry.Rectangle(
                [sub.west, sub.south, sub.east, sub.north]
            )
            sub_prefix = f"{prefix}_tile_{k:04d}"
            tile_paths.append(
                self._download_one_url_tile(image, sub_region, scale, sub_prefix)
            )
        return self._mosaic_tiles(tile_paths, prefix)

    def _export_via_batch(
        self, image, scale: float, region, prefix: str
    ) -> str | TaskInfo:
        """Queue an `ee.batch.Export.image.to{Drive,CloudStorage,Asset}` task.

        When `wait_for_export=True` (the default) blocks until the task
        reaches a terminal state via `wait_for_task` and returns the
        destination URL (`drive://...` / `gs://...` / `ee://...`).
        When `wait_for_export=False` returns a `TaskInfo`
        immediately so the caller can track the task asynchronously via
        `earthlens.gee.jobs`.

        A pinned `crs_transform` is sent as the export's `crsTransform` instead
        of `scale`: Earth Engine's export API rejects both together, so this is
        an either/or rather than a preference.
        """
        common: dict[str, Any] = {
            "image": image,
            "description": prefix[:100],
            "region": region,
            "crs": self.crs,
            "maxPixels": 1e13,
        }
        # `crsTransform` and `scale` are mutually exclusive in Earth Engine's
        # own export API - passing both is an error, not a preference - so a
        # pinned request sends the transform instead.
        crs_transform = self._engine_config.crs_transform
        if crs_transform is None:
            common["scale"] = scale
        else:
            common["crsTransform"] = list(crs_transform)
        nodata = self._export_config.nodata
        if nodata is not None and self._sink.takes_format_options:
            # `GeoTiffImageExportOptions.noData`, the only fill value Earth
            # Engine's API accepts directly. An asset export has no file
            # format, so it declares none - the constructor warns about that.
            common["formatOptions"] = {"noData": nodata}
        # Which constructor, which target keyword and how the destination reads
        # are the sink's business, so they are read off the resolved sink rather
        # than branched on here. The asset sink addresses `assetId` and takes no
        # `fileNamePrefix`, because each export creates one asset rather than a
        # file with a name prefix.
        sink = self._sink
        # Only a batch sink reaches here - the caller dispatches on
        # `sink.is_batch` - so narrow to the subclass that declares the export
        # method and the target keyword, rather than reading them off the ABC.
        assert isinstance(sink, _BatchSink)  # the caller guarantees this
        if isinstance(sink, AssetSink):
            # The asset sink addresses `assetId` and takes no `fileNamePrefix`:
            # each export creates one asset rather than a file with a prefix.
            target: dict[str, str] = sink.target(self, prefix)
        else:
            target = {**sink.target(self), "fileNamePrefix": prefix}
        task = getattr(ee.batch.Export.image, sink.export_method)(**target, **common)
        destination = sink.destination(self, prefix)
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
        `earthlens.gee.features.create_feature`) when given, else a
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
            var_info: The catalog entry (its `Extent`).

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
            `_discover_ee_extent` (cached per asset for the
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
        """Cached entry point for `_discover_ee_extent`.

        Reads / writes `_EXTENT_CACHE` (module-level) so a
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
        `_maybe_discover_ee_extent`). On any EE-side failure
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
