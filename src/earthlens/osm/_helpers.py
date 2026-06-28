"""Pure bbox + result-conversion helpers for the OpenStreetMap backend.

Three concerns are factored here so `osm/backend.py` only routes:

* the bbox-order helpers `bbox_swne` / `bbox_wsen` / `shapely_bbox` —
  `SpatialExtent` exposes the bbox as `.south/.west/.north/.east` but has no
  `bbox_swne` / `shapely_bbox` of its own, and the two protocols want the
  corners in *different* orders (`G3`): Overpass QL is `S,W,N,E`, the ohsome
  `bboxes` parameter is `W,S,E,N`.
* `overpy_to_gdf` — overpy returns parsed elements, not a `GeoDataFrame`, so
  geometry is built here (`G4`). Under the `out geom;` QL the backend uses,
  way coordinates ride on `way.attributes["geometry"]` (a list of `{lat, lon}`
  dicts), *not* on `way.nodes` (which raises `DataIncomplete` under
  `out geom`) — see `planning/osm/captures/osm-sdk-facts.md`.
* `to_fc` / `empty_fc` — wrap a `GeoDataFrame` into a pyramids
  `FeatureCollection` (`G7`), normalising the CRS to EPSG:4326. The ohsome
  path's `.as_dataframe()` is already a `GeoDataFrame`, so it goes straight to
  `to_fc`.

All GIS containerisation stays inside the pyramids `FeatureCollection` per the
repository's pyramids policy; earthlens only assembles the plain attribute rows
and the shapely geometry column. `LicenseWarning` (`G5`) is re-exported from
the shared biodiversity home so the backend imports the one warning class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import LineString, Point, Polygon, box

# `LicenseWarning` is shared across the ODbL / restrictive-license backends; it
# lives in the biodiversity cluster's helper module (overture re-exports the same
# class object) and is re-exported here so the backend imports it from its own
# subpackage.
from earthlens.biodiversity._helpers import LicenseWarning  # noqa: F401

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

#: WGS84 — the CRS every OSM FeatureCollection is tagged with.
OSM_CRS = "EPSG:4326"

#: The minimal identity columns every overpy-built row carries, ahead of the
#: element's own tags.
_ID_COLUMNS = ["osm_id", "osm_type"]


def bbox_swne(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the bbox in Overpass QL order `(south, west, north, east)`.

    Args:
        space: A spatial extent exposing `.south`, `.west`, `.north`, and
            `.east` float properties (typically `self.space` on a backend).

    Returns:
        tuple[float, float, float, float]: `(south, west, north, east)`.

    Examples:
        - Overpass wants `S,W,N,E`:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import bbox_swne
            >>> extent = SpatialExtent.from_pairs(lat_lim=(49.40, 49.42), lon_lim=(8.67, 8.71))
            >>> bbox_swne(extent)
            (49.4, 8.67, 49.42, 8.71)

            ```
    """
    return (space.south, space.west, space.north, space.east)


def bbox_wsen(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the bbox in ohsome `bboxes` order `(west, south, east, north)`.

    Args:
        space: A spatial extent exposing `.west`, `.south`, `.east`, and
            `.north` float properties.

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)`.

    Examples:
        - ohsome wants `W,S,E,N`:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import bbox_wsen
            >>> extent = SpatialExtent.from_pairs(lat_lim=(49.40, 49.42), lon_lim=(8.67, 8.71))
            >>> bbox_wsen(extent)
            (8.67, 49.4, 8.71, 49.42)

            ```
    """
    return (space.west, space.south, space.east, space.north)


def shapely_bbox(space: SpatialExtent):
    """Return a shapely `box` for the extent (for any client-side geometry filter).

    Args:
        space: A spatial extent exposing `.west`, `.south`, `.east`, and
            `.north` float properties.

    Returns:
        shapely.geometry.Polygon: `box(west, south, east, north)`.

    Examples:
        - The box spans the requested corners:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import shapely_bbox
            >>> extent = SpatialExtent.from_pairs(lat_lim=(0.0, 2.0), lon_lim=(0.0, 1.0))
            >>> shapely_bbox(extent).bounds
            (0.0, 0.0, 1.0, 2.0)

            ```
    """
    return box(space.west, space.south, space.east, space.north)


def _way_geometry(way) -> Polygon | LineString | None:
    """Build a way's geometry from its inline `out geom;` coordinates.

    overpy stores the `out geom;` coordinates on `way.attributes["geometry"]`
    (a list of `{"lat": Decimal, "lon": Decimal}` dicts); `way.nodes` is
    unavailable under that QL. A closed ring (first point equals last, at least
    four points) becomes a `Polygon`; two or more points otherwise become a
    `LineString`; fewer than two points yields `None` (the row is skipped).

    Args:
        way: An overpy `Way` (or any object with the same `attributes` shape).

    Returns:
        Polygon | LineString | None: The way geometry, or `None` when there
            are too few coordinates to build one.
    """
    geom = way.attributes.get("geometry") or []
    coords = [(float(point["lon"]), float(point["lat"])) for point in geom]
    if len(coords) >= 4 and coords[0] == coords[-1]:
        return Polygon(coords)
    if len(coords) >= 2:
        return LineString(coords)
    return None


def _row(osm_id: int, osm_type: str, geometry, tags: dict) -> dict:
    """Build one element row with the reserved identity/geometry keys winning.

    The element's free-form OSM `tags` are spread first so a tag whose key
    collides with a reserved column (`osm_id`, `osm_type`, `geometry`) cannot
    clobber the identity or geometry value — the reserved keys are written last.

    Args:
        osm_id: The OSM element id.
        osm_type: `"node"` or `"way"`.
        geometry: The shapely geometry built for the element.
        tags: The element's OSM tags.

    Returns:
        dict: The row mapping, reserved keys last.
    """
    return {**dict(tags), "osm_id": osm_id, "osm_type": osm_type, "geometry": geometry}


def overpy_to_gdf(result) -> gpd.GeoDataFrame:
    """Build a WGS84 `GeoDataFrame` from a parsed overpy result.

    overpy returns parsed `.nodes` / `.ways` / `.relations` rather than a
    `GeoDataFrame`, so geometry is assembled here (`G4`). Nodes become
    `Point(lon, lat)`; ways become a `Polygon` (closed ring) or `LineString`
    from their inline `out geom;` coordinates (see `_way_geometry`). Each
    element's `tags` are spread as columns alongside `osm_id` / `osm_type`.
    Relations are skipped in the MVP (member-geometry assembly is out of
    scope; documented in the OSM docs).

    Args:
        result: A parsed overpy result exposing `.nodes`, `.ways`, and
            `.relations` (e.g. from `overpy.Overpass().parse_json(...)`).

    Returns:
        geopandas.GeoDataFrame: One row per node/way with a built geometry,
            CRS `EPSG:4326`. Empty (schema-only) when nothing matched.

    Examples:
        - One node and one closed way become a Point and a Polygon:
            ```python
            >>> from types import SimpleNamespace as NS
            >>> from earthlens.osm import overpy_to_gdf
            >>> node = NS(id=1, lat=49.41, lon=8.69, tags={"amenity": "cafe"})
            >>> way = NS(
            ...     id=2,
            ...     tags={"building": "yes"},
            ...     attributes={"geometry": [
            ...         {"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 1.0},
            ...         {"lat": 1.0, "lon": 1.0}, {"lat": 0.0, "lon": 0.0},
            ...     ]},
            ... )
            >>> result = NS(nodes=[node], ways=[way], relations=[])
            >>> gdf = overpy_to_gdf(result)
            >>> sorted(gdf.geometry.geom_type)
            ['Point', 'Polygon']
            >>> str(gdf.crs)
            'EPSG:4326'

            ```
    """
    rows: list[dict] = []
    for node in result.nodes:
        rows.append(
            _row(
                node.id,
                "node",
                Point(float(node.lon), float(node.lat)),
                node.tags,
            )
        )
    for way in result.ways:
        geometry = _way_geometry(way)
        if geometry is None:
            continue
        rows.append(_row(way.id, "way", geometry, way.tags))
    if not rows:
        return _empty_gdf()
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=OSM_CRS)


def to_fc(gdf: gpd.GeoDataFrame) -> FeatureCollection:
    """Wrap a `GeoDataFrame` as a `FeatureCollection`, normalising CRS to EPSG:4326.

    Used for both protocols (`G7`): the ohsome path's `.as_dataframe()` is
    already a `GeoDataFrame`, and the Overpass path's `overpy_to_gdf` builds
    one. A frame whose CRS is unset is tagged EPSG:4326; a frame in another CRS
    is reprojected.

    Args:
        gdf: The features to wrap (EPSG:4326, another CRS, or no CRS set).

    Returns:
        FeatureCollection: The features in EPSG:4326.

    Examples:
        - A CRS-less frame is tagged EPSG:4326:
            ```python
            >>> import geopandas as gpd
            >>> from shapely.geometry import Point
            >>> from earthlens.osm import to_fc
            >>> gdf = gpd.GeoDataFrame({"osm_id": [1]}, geometry=[Point(0, 0)])
            >>> fc = to_fc(gdf)
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    if gdf.crs is None:
        gdf = gdf.set_crs(OSM_CRS)
    elif gdf.crs.to_epsg() != 4326:
        # Compare by EPSG code, not string: a 4326-equivalent CRS expressed
        # differently (OGC:CRS84, a WKT-built CRS) should not trigger a
        # redundant reprojection.
        gdf = gdf.to_crs(OSM_CRS)
    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the minimal OSM schema.

    Used for a query that matched nothing, so callers always get a collection
    with an `osm_id` / `osm_type` / `geometry` schema back regardless of hit
    count.

    Returns:
        FeatureCollection: Zero rows, columns `osm_id` / `osm_type` plus an
            empty `geometry` column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.osm import empty_fc
            >>> fc = empty_fc()
            >>> len(fc)
            0
            >>> "osm_type" in fc.columns
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    return FeatureCollection(_empty_gdf())


def _empty_gdf() -> gpd.GeoDataFrame:
    """Return an empty WGS84 `GeoDataFrame` carrying the minimal OSM schema.

    Returns:
        geopandas.GeoDataFrame: Zero rows, columns `osm_id` / `osm_type`, an
            empty `geometry` column, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame(
        {column: pd.Series([], dtype="object") for column in _ID_COLUMNS}
    )
    return gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs=OSM_CRS), crs=OSM_CRS)
