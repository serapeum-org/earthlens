"""Backend-agnostic spatial helpers.

Small pure-Python utilities that act on geographic bounding boxes and
are useful across every concrete data-source backend (GEE, ECMWF, CHC,
S3, ...). Kept here rather than in any one backend so a new backend
doesn't have to reach into `gee/_helpers.py` for them. Eventual home
is the sibling pyramids GIS package — keep the free-function shape
that's already pyramids-compatible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from loguru import logger
from pyramids.base.remote import CloudConfig
from pyramids.feature import bbox as _pyramids_bbox

#: `/vsicurl` HTTP tuning for a remote-raster read: suppress per-open sidecar
#: probes (`GDAL_DISABLE_READDIR_ON_OPEN`, via `vsicurl_tuning`) and bound each
#: range request with a retry / timeout budget. These are the knobs the raster
#: backends used to set by hand; a plain `Dataset.read_file` installs none.
_VSICURL_HTTP_MAX_RETRY = 3
_VSICURL_HTTP_RETRY_DELAY = 2.0
_VSICURL_HTTP_TIMEOUT = 30

#: Approximate metres per degree of latitude at the equator. Retained as
#: part of the public surface for callers doing their own rough
#: degree-to-metre sizing. :func:`estimate_pixel_dims` no longer reads it
#: — the sizing math now delegates to `pyramids.feature.bbox`, which uses
#: the polar-maximum 111_694 m for the latitude axis (a slightly larger,
#: safer over-count for a size guard).
METRES_PER_DEGREE: float = 111_320.0


def estimate_pixel_dims(
    west: float,
    south: float,
    east: float,
    north: float,
    scale_m: float,
) -> tuple[int, int]:
    """Estimate the (width, height) in pixels of a WGS84 bbox at `scale_m`.

    A rough estimate suitable for pre-flight size guards on raster
    downloads. The sizing math is pyramids' — this is a thin
    edge-argument adapter over `pyramids.feature.bbox.estimate_pixel_dims`,
    which takes a `(west, south, east, north)` tuple. Both axes are
    over-counted (longitude with the equatorial constant, latitude with
    the polar-maximum one), which is the safe direction for a guard. For
    an exact geodesic computation use pyproj's `Geod.inv` instead.

    The edge guards below stay here rather than deferring to pyramids:
    pyramids reads `east < west` as an **antimeridian-crossing** bbox and
    happily returns a globe-spanning width, whereas earthlens's
    `SpatialExtent` forbids `west > east` outright — so for this package
    an inverted bbox is a caller bug to surface, not a dateline crossing
    to honour.

    Args:
        west: Western edge of the bbox in degrees longitude.
        south: Southern edge of the bbox in degrees latitude.
        east: Eastern edge of the bbox in degrees longitude.
        north: Northern edge of the bbox in degrees latitude.
        scale_m: Output pixel size in metres.

    Returns:
        `(width_px, height_px)` — both rounded up to the next integer,
        each at least 1.

    Raises:
        ValueError: If `scale_m` is not positive or `east < west` /
            `north < south`.

    Examples:
        - A 0.1° × 0.1° box at 90 m is tiny:
            ```python
            >>> estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 90.0)
            (124, 125)

            ```
        - The same box at 10 m is ~9× larger per axis:
            ```python
            >>> estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 10.0)
            (1114, 1117)

            ```
    """
    if scale_m <= 0:
        raise ValueError(f"scale_m must be positive, got {scale_m}")
    if east < west:
        raise ValueError(f"east ({east}) < west ({west})")
    if north < south:
        raise ValueError(f"north ({north}) < south ({south})")
    return cast(
        "tuple[int, int]",
        _pyramids_bbox.estimate_pixel_dims((west, south, east, north), scale_m),
    )


#: Accepted spellings for each edge of a bounding-box mapping are owned by
#: `pyramids.feature.bbox.read_bbox_dict` — the GeoJSON / pyramids `min_lon`
#: form, the eodag `lonmin` form, the shapely / geopandas `minx` form, and
#: the compass `west` form, so a caller's bbox dict is read whatever
#: convention they reach for. `tests/base/test_aoi.py` pins the spellings
#: earthlens's AOI channel promises, so an upstream change to that set fails
#: here rather than silently narrowing what a caller may pass.


def _coords_bounds(coords: Any) -> tuple[float, float, float, float]:
    """Return `(min_lon, min_lat, max_lon, max_lat)` of a GeoJSON coordinate tree.

    Walks the arbitrarily nested coordinate arrays of any GeoJSON
    geometry (Point through MultiPolygon) and collects every `(lon, lat)`
    position, ignoring any third (elevation) ordinate.

    Args:
        coords: The `coordinates` value of a GeoJSON geometry — a single
            position, or a list nested to any depth ending in positions.

    Returns:
        The `(min_lon, min_lat, max_lon, max_lat)` envelope.

    Raises:
        ValueError: If no `(lon, lat)` position is found.
    """
    xs: list[float] = []
    ys: list[float] = []
    stack: list[Any] = [coords]
    while stack:
        item = stack.pop()
        if (
            isinstance(item, Sequence)
            and not isinstance(item, str)
            and len(item) >= 2
            and all(isinstance(c, (int, float)) for c in item[:2])
        ):
            xs.append(float(item[0]))
            ys.append(float(item[1]))
        elif isinstance(item, Sequence) and not isinstance(item, str):
            stack.extend(item)
    if not xs:
        raise ValueError("aoi geometry has no coordinates")
    return min(xs), min(ys), max(xs), max(ys)


def _geojson_bounds(obj: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Return the `(min_lon, min_lat, max_lon, max_lat)` envelope of a GeoJSON mapping.

    Handles a bare geometry, a `Feature`, a `FeatureCollection`, a
    `GeometryCollection`, or any mapping carrying a precomputed GeoJSON
    `bbox` member (2-D `[W, S, E, N]` or 3-D `[W, S, zmin, E, N, zmax]`).

    Args:
        obj: A GeoJSON-like mapping (e.g. a dict, or the result of an
            object's `__geo_interface__`).

    Returns:
        The `(min_lon, min_lat, max_lon, max_lat)` envelope.

    Raises:
        ValueError: If the mapping is not a recognisable GeoJSON shape.
    """
    bbox = obj.get("bbox")
    if bbox:
        if len(bbox) == 4:
            return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        if len(bbox) == 6:
            return float(bbox[0]), float(bbox[1]), float(bbox[3]), float(bbox[4])
    geo_type = obj.get("type")
    if geo_type == "Feature":
        return _geojson_bounds(obj["geometry"])
    if geo_type in ("FeatureCollection", "GeometryCollection"):
        members = (
            obj["features"] if geo_type == "FeatureCollection" else obj["geometries"]
        )
        boxes = [_geojson_bounds(member) for member in members]
        if not boxes:
            raise ValueError(f"empty {geo_type} cannot define an aoi")
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
    if "coordinates" in obj:
        return _coords_bounds(obj["coordinates"])
    raise ValueError("aoi mapping is not a recognised GeoJSON geometry")


def _bbox_dict_bounds(obj: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Return `(min_lon, min_lat, max_lon, max_lat)` from a bbox mapping.

    A thin pass-through to `pyramids.feature.bbox.read_bbox_dict`, which
    reads each edge by trying every accepted spelling in turn.

    Args:
        obj: A mapping with one spelling of each edge (e.g.
            `{"min_lon": .., "min_lat": .., "max_lon": .., "max_lat": ..}`).

    Returns:
        The `(min_lon, min_lat, max_lon, max_lat)` envelope.

    Raises:
        ValueError: If any edge is missing under every accepted spelling.
            The message names the missing edge by its compass spelling
            (`'north'`), whichever convention the caller used for the keys.
    """
    return cast("tuple[float, float, float, float]", _pyramids_bbox.read_bbox_dict(obj))


_POLYGONAL_GEOM_TYPES = frozenset({"Polygon", "MultiPolygon"})


def _polygon_or_none(shape: Any) -> Any:
    """Return `shape` if it is a (multi)polygon, else `None`.

    A point, line, or empty geometry has no area to mask with, so the
    request falls back to a plain bbox clip.

    Args:
        shape: A shapely geometry (anything exposing `geom_type`).

    Returns:
        The geometry when it is a `Polygon` / `MultiPolygon`, else `None`.
    """
    return shape if getattr(shape, "geom_type", None) in _POLYGONAL_GEOM_TYPES else None


def _geojson_polygon(obj: Mapping[str, Any]) -> Any:
    """Return a shapely (multi)polygon for a GeoJSON mapping, or `None`.

    Mirrors :func:`_geojson_bounds`'s structural handling (bare geometry,
    `Feature`, `FeatureCollection`) but yields the dissolved polygon
    geometry rather than its envelope. Non-polygonal or unparseable input
    returns `None` so the caller clips to the bbox instead.

    Args:
        obj: A GeoJSON-like mapping.

    Returns:
        A shapely `Polygon` / `MultiPolygon` (the union, for collections),
        or `None`.
    """
    try:
        from shapely.geometry import shape as _shape
        from shapely.ops import unary_union
    except ImportError:
        return None
    geo_type = obj.get("type")
    if geo_type == "Feature":
        geometry = obj.get("geometry")
        return _geojson_polygon(geometry) if geometry else None
    if geo_type in ("FeatureCollection", "GeometryCollection"):
        key = "features" if geo_type == "FeatureCollection" else "geometries"
        geoms = [_geojson_polygon(m) for m in obj.get(key, [])]
        geoms = [g for g in geoms if g is not None]
        return _polygon_or_none(unary_union(geoms)) if geoms else None
    if "coordinates" not in obj:
        return None
    try:
        return _polygon_or_none(_shape(obj))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _to_clip_gdf(geom: Any) -> Any:
    """Wrap a polygon mask as a WGS84 `GeoDataFrame`, or pass through `None`.

    Accepts a shapely geometry, a `GeoSeries`, or an already-built
    `GeoDataFrame` and returns a single-CRS `GeoDataFrame` suitable as the
    `mask=` argument of `pyramids.Dataset.crop`. A frame with no declared
    CRS is taken as WGS84. Returns `None` when `geom` is `None` or when
    `geopandas` is not installed (the request then clips to the bbox).

    Args:
        geom: A shapely (multi)polygon, `GeoSeries`, `GeoDataFrame`, or
            `None`.

    Returns:
        A WGS84 `GeoDataFrame`, or `None`.
    """
    if geom is None:
        return None
    try:
        import geopandas as gpd
    except ImportError:
        return None
    if isinstance(geom, gpd.GeoDataFrame):
        gdf = geom
    elif isinstance(geom, gpd.GeoSeries):
        gdf = gpd.GeoDataFrame(geometry=geom.reset_index(drop=True))
    else:
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def resolve_aoi(
    aoi: Any, buffer: float | None = None
) -> tuple[list[float], list[float], Any]:
    """Coerce a flexible area-of-interest into `(lat_lim, lon_lim, geometry)`.

    The same flexible `aoi` channel as :func:`normalize_aoi`, but it also
    returns the polygon mask when the area of interest had a real
    (non-rectangular) shape. Raster backends that clip via
    `pyramids.Dataset.crop` use that mask to clip the fetched bbox to the
    exact polygon; for bbox / point inputs the geometry is `None` and a
    plain bbox clip is exact. See :func:`normalize_aoi` for the accepted
    `aoi` forms and the bbox semantics.

    Args:
        aoi: The area of interest in any of the accepted forms.
        buffer: Half-width in degrees for the `(lon, lat)` point form.

    Returns:
        `(lat_lim, lon_lim, geometry)` where the pairs are `[min, max]`
        floats in degrees and `geometry` is a WGS84 `GeoDataFrame` polygon
        mask (or `None` for a bbox / point aoi, or when `geopandas` is
        unavailable).

    Raises:
        ValueError: If `aoi` is malformed, a point is given without
            `buffer`, or the resulting box is degenerate / inverted.
        TypeError: If `aoi` is of an unsupported type.
    """
    geom: Any = None
    if isinstance(aoi, str):
        from shapely import wkt

        shape = wkt.loads(aoi)
        min_lon, min_lat, max_lon, max_lat = shape.bounds
        geom = _polygon_or_none(shape)
    elif isinstance(aoi, Mapping):
        if "type" in aoi or "coordinates" in aoi or "bbox" in aoi:
            min_lon, min_lat, max_lon, max_lat = _geojson_bounds(aoi)
            geom = _geojson_polygon(aoi)
        else:
            min_lon, min_lat, max_lon, max_lat = _bbox_dict_bounds(aoi)
    elif hasattr(aoi, "total_bounds"):  # geopandas GeoDataFrame / GeoSeries
        # `total_bounds` is in the frame's own CRS; reproject to WGS84
        # lon/lat first so a projected frame (e.g. UTM / Web Mercator)
        # does not yield a metre-valued, out-of-range bbox. A frame with
        # no CRS is taken as-is (already lon/lat).
        crs = getattr(aoi, "crs", None)
        if crs is not None and crs.to_epsg() != 4326:
            aoi = aoi.to_crs(4326)
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in aoi.total_bounds)
        geom_type = getattr(aoi, "geom_type", None)
        if geom_type is not None and any(
            t in _POLYGONAL_GEOM_TYPES for t in geom_type.unique()
        ):
            geom = aoi
    elif hasattr(aoi, "__geo_interface__"):  # shapely geometry, etc.
        gi = aoi.__geo_interface__
        min_lon, min_lat, max_lon, max_lat = _geojson_bounds(gi)
        geom = _geojson_polygon(gi)
    elif isinstance(aoi, Sequence) and not isinstance(aoi, str):
        if len(aoi) == 4:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in aoi)
        elif len(aoi) == 2:
            if buffer is None:
                raise ValueError(
                    "a point aoi=(lon, lat) requires buffer= (a half-width "
                    "in degrees) to define an area"
                )
            lon, lat = float(aoi[0]), float(aoi[1])
            min_lon, max_lon = lon - buffer, lon + buffer
            min_lat, max_lat = lat - buffer, lat + buffer
            min_lat, max_lat = max(min_lat, -90.0), min(max_lat, 90.0)
            min_lon, max_lon = max(min_lon, -180.0), min(max_lon, 180.0)
        else:
            raise ValueError(
                f"a bbox aoi must have 4 values [W, S, E, N] or a point 2 "
                f"values [lon, lat]; got {len(aoi)}"
            )
    else:
        raise TypeError(
            f"unsupported aoi type {type(aoi).__name__}; pass a bbox "
            "[W, S, E, N], a (lon, lat) point with buffer=, a shapely "
            "geometry / GeoJSON / WKT, or a GeoDataFrame"
        )

    if min_lon > max_lon:
        # West-of-east is the GeoJSON / STAC spelling of an antimeridian
        # crossing rather than a typo, so name the case and the remedy.
        raise ValueError(
            f"aoi has west ({min_lon}) east of east ({max_lon}), which denotes "
            f"an antimeridian crossing. Split it at ±180 and pass the two "
            f"halves as separate requests (e.g. aoi=[{min_lon}, {min_lat}, "
            f"180, {max_lat}] and aoi=[-180, {min_lat}, {max_lon}, {max_lat}])."
        )
    if min_lat > max_lat:
        raise ValueError(
            f"aoi has inverted latitude bounds: [{min_lat}, {max_lat}] "
            f"(south edge north of the north edge)."
        )
    return [min_lat, max_lat], [min_lon, max_lon], _to_clip_gdf(geom)


def normalize_aoi(
    aoi: Any, buffer: float | None = None
) -> tuple[list[float], list[float]]:
    """Coerce a flexible area-of-interest into `(lat_lim, lon_lim)` pairs.

    The single `aoi` channel accepts every shape the popular EO packages
    accept, so a caller never has to remember EarthLens's legacy
    lat-then-lon two-list convention. Accepted forms:

    * a bbox sequence `[min_lon, min_lat, max_lon, max_lat]` — the
      GeoJSON / STAC **W, S, E, N** order;
    * a bbox mapping with any spelling of the four edges (`min_lon` /
      `lonmin` / `minx` / `west`, …);
    * a `(lon, lat)` point — requires `buffer` (a half-width in degrees),
      which is grown into a square box;
    * a shapely geometry, or any object exposing `__geo_interface__`
      (e.g. a `geopandas` row), reduced to its envelope;
    * a GeoJSON geometry / `Feature` / `FeatureCollection` mapping;
    * a WKT string (parsed with shapely);
    * a `GeoDataFrame` / `GeoSeries` (via its `total_bounds`).

    All coordinates are assumed to be WGS84 degrees. The returned pairs
    use the internal `[min, max]` shape that every backend's
    `_create_grid` already consumes, so no backend has to change. This is
    the bbox-only view of :func:`resolve_aoi`; use that when you also need
    the polygon mask for precise (non-rectangular) clipping.

    Args:
        aoi: The area of interest in any of the accepted forms above.
        buffer: Half-width in degrees. Required for, and only used by,
            the `(lon, lat)` point form; a buffered point near a pole is
            clamped to the valid latitude / longitude ranges.

    Returns:
        `(lat_lim, lon_lim)` where each is a `[min, max]` float pair in
        degrees.

    Raises:
        ValueError: If `aoi` is malformed, a point is given without
            `buffer`, or the resulting box is degenerate / inverted.
        TypeError: If `aoi` is of an unsupported type.

    Examples:
        - A bbox list is read as W, S, E, N:
            ```python
            >>> normalize_aoi([-75.0, 4.0, -74.0, 5.0])
            ([4.0, 5.0], [-75.0, -74.0])

            ```
        - A bbox mapping accepts several spellings:
            ```python
            >>> normalize_aoi(
            ...     {"min_lon": -75, "min_lat": 4, "max_lon": -74, "max_lat": 5}
            ... )
            ([4.0, 5.0], [-75.0, -74.0])

            ```
        - A point grows into a square box with `buffer`:
            ```python
            >>> normalize_aoi((-74.5, 4.5), buffer=0.5)
            ([4.0, 5.0], [-75.0, -74.0])

            ```
        - A WKT polygon is reduced to its envelope:
            ```python
            >>> normalize_aoi("POLYGON ((-75 4, -74 4, -74 5, -75 5, -75 4))")
            ([4.0, 5.0], [-75.0, -74.0])

            ```
    """
    lat_lim, lon_lim, _geometry = resolve_aoi(aoi, buffer=buffer)
    return lat_lim, lon_lim


def vsicurl_config() -> CloudConfig:
    """Return a pyramids `CloudConfig` that tunes a remote `/vsicurl` read.

    A plain `pyramids.dataset.Dataset.read_file(url)` installs no GDAL config, so
    the readdir-suppression and retry / timeout knobs a backend needs on the
    remote-raster hot path are not applied by default. Wrap the `read_file` (and
    the `crop` that reads the window) in this context so `/vsicurl` opens skip the
    per-open `.aux.xml` / `.ovr` sidecar probes against the host and bound each
    range request — restoring the tuning the backends used to set by hand, now
    scoped to the read rather than mutating the process environment.

    Returns:
        A `CloudConfig` context manager enabling `vsicurl_tuning` plus the retry /
        retry-delay / timeout budget.

    Examples:
        - Use it around a remote read so the window fetch is tuned:
            ```python
            >>> from earthlens.base.spatial import vsicurl_config
            >>> cfg = vsicurl_config()
            >>> hasattr(cfg, "__enter__")
            True

            ```
    """
    return CloudConfig(
        vsicurl_tuning=True,
        http_max_retry=_VSICURL_HTTP_MAX_RETRY,
        http_retry_delay=_VSICURL_HTTP_RETRY_DELAY,
        http_timeout=_VSICURL_HTTP_TIMEOUT,
    )


def bbox_overlaps(dataset: Any, bbox: Sequence[float]) -> bool:
    """Whether a bbox intersects a dataset's geographic extent.

    A cheap geographic bounds test from the dataset's affine transform and pixel
    dimensions, used to reject an AOI outside the source's coverage *before* a
    windowed read — so an out-of-extent AOI fails fast with a clear error rather
    than falling into pyramids' full-source cutline warp. The bbox is assumed to
    be in the dataset's own CRS (the case for the raster backends here).

    Args:
        dataset: An opened `pyramids.Dataset` exposing `geotransform`, `columns`,
            and `rows`.
        bbox: `(west, south, east, north)` in the dataset's CRS.

    Returns:
        `True` when the bbox intersects the raster's extent.
    """
    origin_x, pixel_w, _, origin_y, _, pixel_h = dataset.geotransform
    west_bound, east_bound = origin_x, origin_x + dataset.columns * pixel_w
    # `pixel_h` is negative for a north-up grid, so the south edge is lower.
    north_bound, south_bound = origin_y, origin_y + dataset.rows * pixel_h
    west, south, east, north = bbox
    return not (
        east <= west_bound
        or west >= east_bound
        or north <= south_bound
        or south >= north_bound
    )


def windowed_bbox_crop(dataset: Any, bbox: Sequence[float], *, epsg: Any = 4326) -> Any:
    """Windowed bbox crop that keeps an all-no-data AOI as an all-no-data crop.

    `Dataset.crop(bbox=)` with the default `touch=True` takes pyramids' windowed
    fast path — reading only the AOI's pixel window — but *raises* `crop produced
    no valid pixels` when that window is entirely no-data. An in-coverage but
    empty AOI (e.g. open sea, or an area with no modelled value) must still
    produce a crop, matching the pre-refactor contract where the backend wrote an
    all-no-data raster rather than aborting. So retry with `touch=False`, whose
    cutline warp crops the same window but returns the all-no-data result instead
    of raising, and materialise it (a read into an in-memory dataset) so the
    fallback's reads happen here — inside any tuning context and before the source
    handle is closed — rather than lazily through a VRT afterwards.

    Contract: the caller must guarantee `bbox` overlaps the source (e.g. via
    `bbox_overlaps`). pyramids raises the same "no valid pixels" message for both
    an all-no-data window (kept here) and a non-overlapping bbox (a full-source
    read then error); pre-checking overlap ensures the only meaning that reaches
    the fallback is all-no-data.

    Args:
        dataset: An opened `pyramids.Dataset` (anything exposing `crop`).
        bbox: `(west, south, east, north)` in `epsg` (already widened if a point),
            guaranteed to overlap the source.
        epsg: CRS of `bbox`. Defaults to `4326`.

    Returns:
        The cropped `Dataset` — the windowed read, or the all-no-data window when
        the AOI holds no valid data.
    """
    try:
        return dataset.crop(bbox=list(bbox), epsg=epsg)
    except ValueError as exc:
        # The only ValueError carrying this phrase is pyramids'
        # `_correct_wrap_cutline_error`; it means the window overlaps no valid
        # data. Coupled to pyramids' wording — the `test_all_nodata_*` tests fail
        # if a pyramids bump rewords it, flagging this string for update.
        if "no valid pixels" not in str(exc):
            raise
        logger.info(
            "windowed crop AOI holds no valid data; keeping the all-no-data "
            "window via the slower cutline-warp crop of the same window"
        )
        fallback = dataset.crop(bbox=list(bbox), epsg=epsg, touch=False)
        # `touch=False` returns a lazy warp VRT that reads through to the source;
        # materialise it now so those reads happen before the caller closes the
        # source handle (and inside any active tuning context).
        from pyramids.dataset import Dataset, GeoReference

        no_data = fallback.no_data_value
        return Dataset.from_array(
            arr=fallback.read_array(),
            no_data_value=no_data[0] if isinstance(no_data, (list, tuple)) else no_data,
            geo_ref=GeoReference(geo=fallback.geotransform, epsg=fallback.epsg),
        )


def widen_degenerate_bbox(
    bbox: Sequence[float], pixel_width: float, pixel_height: float
) -> list[float]:
    """Widen a zero-width / zero-height AOI to one source pixel.

    `pyramids.Dataset.crop(bbox=)` requires a strictly positive box
    (`west < east and south < north`); a point or cell-edge-aligned AOI
    (`min == max` on an axis, which the facade allows) would otherwise raise. A
    collapsed edge is pushed out by exactly one source pixel so the crop's
    windowed fast path resolves to the single cell containing the point — the
    1x1 window the old floor/ceil pixel math clamped to (`max(1, ...)`). A box
    already positive on both axes is returned unchanged.

    One whole pixel (not a sub-pixel epsilon) is used deliberately: a sub-pixel
    box would fall through the strict `west < east` fast-path check into the
    cutline warp and yield no cells. This assumes the pixel size is not lost to
    float rounding at the coordinate magnitude (`west + abs(pixel) > west`),
    which holds for geographic (|lon| <= 180, pixel ~1e-3) and normal projected
    grids.

    Args:
        bbox: `(west, south, east, north)` in the source CRS.
        pixel_width: The source's pixel width (`geotransform[1]`); the absolute
            value is used, so the sign does not matter.
        pixel_height: The source's pixel height (`geotransform[5]`, negative for
            a north-up grid); the absolute value is used.

    Returns:
        A `[west, south, east, north]` list, with any collapsed axis widened by
        one pixel.

    Examples:
        - A point AOI is widened by one pixel on both axes:
            ```python
            >>> from earthlens.base.spatial import widen_degenerate_bbox
            >>> widen_degenerate_bbox([5.0, -5.0, 5.0, -5.0], 1.0, -1.0)
            [5.0, -5.0, 6.0, -4.0]

            ```
        - A positive box is left unchanged:
            ```python
            >>> from earthlens.base.spatial import widen_degenerate_bbox
            >>> widen_degenerate_bbox([4.8, 51.8, 5.0, 52.0], 0.00083, -0.00083)
            [4.8, 51.8, 5.0, 52.0]

            ```
    """
    west, south, east, north = bbox
    if east <= west:
        east = west + abs(pixel_width)
    if north <= south:
        north = south + abs(pixel_height)
    return [west, south, east, north]


def ensure_no_data(dataset: Any, default: float) -> Any:
    """Stamp a fallback no-data value when a dataset declares none.

    A windowed `crop(bbox=)` carries the source's own no-data through, but a
    source that declares none leaves the output untagged — losing the flag that
    exact polygon masking (`crop_to_aoi`) needs to trim outside-polygon cells.
    When the dataset's first band has no no-data, set `default` and return the
    dataset; this is a pure metadata tag (pixels are unchanged), restoring the
    pre-crop behaviour where the backend stamped a catalog / default no-data.

    Args:
        dataset: A `pyramids.Dataset` (anything exposing a settable
            `no_data_value` per-band tuple).
        default: The no-data value to stamp when the dataset declares none.

    Returns:
        The same `dataset`, with a no-data value guaranteed on its first band.
    """
    nodata = getattr(dataset, "no_data_value", None)
    if not isinstance(nodata, (list, tuple)) or not nodata or nodata[0] is None:
        dataset.no_data_value = default
    return dataset


def crop_to_aoi(
    dataset: Any,
    space: Any,
    *,
    bbox: Sequence[float],
    epsg: Any = 4326,
    touch: bool = False,
) -> Any:
    """Crop a pyramids `Dataset` to a polygon mask if present, else a bbox.

    When `space` carries a polygon `geometry` — set when the request's
    `aoi=` was a polygon rather than a plain bbox — the dataset is masked
    to that exact shape via `Dataset.crop(mask=...)`, so pixels outside the
    polygon become no-data and the raster is trimmed to the polygon's cell
    extent. Otherwise it is cropped to the rectangular `bbox`. Centralising
    the choice lets every raster backend honour a polygon `aoi=` the same
    way without duplicating the branch. On the polygon path a raster that
    declares no no-data value logs a warning, since the mask can then only
    trim to the polygon's bounding box (see `_crop_to_mask`).

    Args:
        dataset: A `pyramids.Dataset` (anything exposing `crop`).
        space: A :class:`~earthlens.base.abstractdatasource.SpatialExtent`
            (anything exposing an optional `geometry`).
        bbox: The fallback `(west, south, east, north)` quadruple, used
            when `space` has no polygon `geometry`.
        epsg: CRS of `bbox`. Defaults to `4326`.
        touch: For the bbox path, whether to keep cells merely touching the
            box. Ignored on the polygon-mask path, which always keeps
            touching cells. Defaults to `False`.

    Returns:
        A new cropped `Dataset`.
    """
    geometry = getattr(space, "geometry", None)
    if geometry is not None:
        return _crop_to_mask(dataset, geometry, touch=True)
    # Widen a point / cell-edge bbox to one pixel so a zero-area box is not handed
    # to `crop` (which would trim to nothing / error); a point AOI still yields a
    # 1x1 crop. The pixel size (`geo[1]`/`geo[5]`) is in the dataset's CRS units,
    # so only widen when the bbox is in that same CRS — `epsg is None` means "the
    # dataset's own CRS" (pyramids' default), and an explicit `epsg` must match
    # the dataset's. For a reprojecting crop (bbox `epsg` != the dataset's CRS)
    # mixing the units would push the edge out by a wrong amount, so leave the
    # bbox as-is. (A real `Dataset` exposes `geotransform` / `epsg`; the getattr
    # guards let a bare test double without them reach crop.)
    geo = getattr(dataset, "geotransform", None)
    if geo is not None and (epsg is None or getattr(dataset, "epsg", None) == epsg):
        bbox = widen_degenerate_bbox(bbox, geo[1], geo[5])
    return dataset.crop(bbox=list(bbox), epsg=epsg, touch=touch)


def _crop_to_mask(dataset: Any, geometry: Any, *, touch: bool) -> Any:
    """Mask a `Dataset` to `geometry`, warning when it has no no-data value.

    `pyramids.Dataset.crop(mask=...)` writes the dataset's no-data value
    into the cells outside the polygon. If the raster declares no no-data
    value, those cells cannot be flagged, so the mask effectively only
    trims the raster to the polygon's bounding box rather than to the exact
    shape. That silent degradation is surfaced as a warning so a caller is
    not misled into thinking a polygon `aoi=` was honoured precisely.

    Args:
        dataset: A `pyramids.Dataset` / `NetCDF` (anything exposing `crop`
            and, ideally, `no_data_value`).
        geometry: A WGS84 `GeoDataFrame` polygon mask.
        touch: Whether to keep cells merely touching the polygon.

    Returns:
        The masked `Dataset`.
    """
    # `no_data_value` is normally a per-band tuple, e.g. `(None,)` /
    # `(-9999.0,)`. Guard on list/tuple so a missing attribute or a bare
    # scalar never trips the all-None iteration.
    nodata = getattr(dataset, "no_data_value", None)
    if isinstance(nodata, (list, tuple)) and all(v is None for v in nodata):
        logger.warning(
            "polygon aoi= mask applied to a raster with no no-data value; "
            "cells outside the polygon but inside its bounding box cannot be "
            "flagged, so the result is trimmed to the polygon's bbox rather "
            "than its exact shape."
        )
    return dataset.crop(mask=geometry, touch=touch)


def mask_to_geometry(dataset: Any, space: Any, *, touch: bool = True) -> Any:
    """Mask an already-bbox-clipped `Dataset` / `NetCDF` to a polygon, if any.

    The counterpart to :func:`crop_to_aoi` for backends that have *already*
    cropped to the bbox by another route — CHIRPS's in-array numpy clip, or
    a server-side bbox (ECMWF's CDS `area`, a NetCDF cube). When `space`
    carries a polygon `geometry`, the dataset is masked to that exact shape
    via `crop(mask=...)`; otherwise it is returned unchanged. As with
    `crop_to_aoi`, masking a raster that declares no no-data value logs a
    warning, since the mask can then only trim to the polygon's bounding box
    (see `_crop_to_mask`).

    Args:
        dataset: A `pyramids.Dataset` / `NetCDF` (anything exposing `crop`).
        space: A :class:`~earthlens.base.abstractdatasource.SpatialExtent`
            (anything exposing an optional `geometry`).
        touch: Whether to keep cells merely touching the polygon. Defaults
            to `True`.

    Returns:
        The masked `Dataset` when a polygon `geometry` is present, else the
        original `dataset` untouched.
    """
    geometry = getattr(space, "geometry", None)
    if geometry is None:
        return dataset
    return _crop_to_mask(dataset, geometry, touch=touch)
