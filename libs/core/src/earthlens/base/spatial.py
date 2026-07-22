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
from pyramids.feature import bbox as _pyramids_bbox

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

    if min_lon > max_lon or min_lat > max_lat:
        raise ValueError(
            f"aoi has inverted bounds: "
            f"lon [{min_lon}, {max_lon}], lat [{min_lat}, {max_lat}]"
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
