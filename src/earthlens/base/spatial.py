"""Backend-agnostic spatial helpers.

Small pure-Python utilities that act on geographic bounding boxes and
are useful across every concrete data-source backend (GEE, ECMWF, CHC,
S3, ...). Kept here rather than in any one backend so a new backend
doesn't have to reach into `gee/_helpers.py` for them. Eventual home
is the sibling pyramids GIS package — keep the free-function shape
that's already pyramids-compatible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

#: Approximate metres per degree of latitude at the equator. Used by
#: :func:`estimate_pixel_dims` for fast pre-flight pixel-grid sizing;
#: slightly over-counts longitude pixels away from the equator (which
#: is the safe direction for a size guard).
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
    downloads. Degrees are converted to metres with the equatorial
    constant :data:`METRES_PER_DEGREE`, so the width is over-counted
    away from the equator — the safe direction for a guard. For an
    exact geodesic computation use pyproj's `Geod.inv` instead.

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
            (124, 124)

            ```
        - The same box at 10 m is ~9× larger per axis:
            ```python
            >>> estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 10.0)
            (1114, 1114)

            ```
    """
    if scale_m <= 0:
        raise ValueError(f"scale_m must be positive, got {scale_m}")
    if east < west:
        raise ValueError(f"east ({east}) < west ({west})")
    if north < south:
        raise ValueError(f"north ({north}) < south ({south})")
    deg_per_px = scale_m / METRES_PER_DEGREE
    width_px = math.ceil((east - west) / deg_per_px)
    height_px = math.ceil((north - south) / deg_per_px)
    return max(width_px, 1), max(height_px, 1)


#: Accepted spellings for each edge of a bounding-box mapping, tried in
#: order. Covers the GeoJSON / pyramids `min_lon` form, the eodag
#: `lonmin` form, the shapely / geopandas `minx` form, and the compass
#: `west` form so a caller's bbox dict is read whatever convention they
#: reach for.
_BBOX_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "min_lon": ("min_lon", "lonmin", "minlon", "minx", "west"),
    "min_lat": ("min_lat", "latmin", "minlat", "miny", "south"),
    "max_lon": ("max_lon", "lonmax", "maxlon", "maxx", "east"),
    "max_lat": ("max_lat", "latmax", "maxlat", "maxy", "north"),
}


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
        members = obj["features"] if geo_type == "FeatureCollection" else obj["geometries"]
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

    Reads the four edges by trying each alias in :data:`_BBOX_KEY_ALIASES`.

    Args:
        obj: A mapping with one spelling of each edge (e.g.
            `{"min_lon": .., "min_lat": .., "max_lon": .., "max_lat": ..}`).

    Returns:
        The `(min_lon, min_lat, max_lon, max_lat)` envelope.

    Raises:
        ValueError: If any edge is missing under every accepted spelling.
    """
    edges: dict[str, float] = {}
    for canonical, aliases in _BBOX_KEY_ALIASES.items():
        for alias in aliases:
            if alias in obj:
                edges[canonical] = float(obj[alias])
                break
        else:
            raise ValueError(
                f"aoi bbox mapping is missing {canonical!r} "
                f"(accepted spellings: {list(aliases)})"
            )
    return edges["min_lon"], edges["min_lat"], edges["max_lon"], edges["max_lat"]


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
    `_create_grid` already consumes, so no backend has to change.

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
    if isinstance(aoi, str):
        from shapely import wkt

        min_lon, min_lat, max_lon, max_lat = wkt.loads(aoi).bounds
    elif isinstance(aoi, Mapping):
        if "type" in aoi or "coordinates" in aoi or "bbox" in aoi:
            min_lon, min_lat, max_lon, max_lat = _geojson_bounds(aoi)
        else:
            min_lon, min_lat, max_lon, max_lat = _bbox_dict_bounds(aoi)
    elif hasattr(aoi, "total_bounds"):  # geopandas GeoDataFrame / GeoSeries
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in aoi.total_bounds)
    elif hasattr(aoi, "__geo_interface__"):  # shapely geometry, etc.
        min_lon, min_lat, max_lon, max_lat = _geojson_bounds(aoi.__geo_interface__)
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
    return [min_lat, max_lat], [min_lon, max_lon]
