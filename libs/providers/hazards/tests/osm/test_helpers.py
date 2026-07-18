"""Unit tests for the OSM bbox + result-conversion helpers."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from earthlens.base import SpatialExtent
from earthlens.osm._helpers import (
    OSM_CRS,
    bbox_swne,
    bbox_wsen,
    empty_fc,
    overpy_to_gdf,
    shapely_bbox,
    to_fc,
)
from .conftest import FakeResult, make_result

pytestmark = pytest.mark.osm


@pytest.fixture
def extent() -> SpatialExtent:
    """A small WGS84 extent over central Heidelberg."""
    return SpatialExtent.from_pairs(lat_lim=[49.40, 49.42], lon_lim=[8.67, 8.71])


class TestBboxHelpers:
    """The two protocols' bbox-order helpers and the shapely box."""

    def test_bbox_swne_is_south_west_north_east(self, extent):
        """Overpass order is (south, west, north, east)."""
        assert bbox_swne(extent) == (49.40, 8.67, 49.42, 8.71)

    def test_bbox_wsen_is_west_south_east_north(self, extent):
        """ohsome order is (west, south, east, north)."""
        assert bbox_wsen(extent) == (8.67, 49.40, 8.71, 49.42)

    def test_shapely_bbox_matches_corners(self, extent):
        """The shapely box spans (west, south, east, north)."""
        assert shapely_bbox(extent).bounds == (8.67, 49.40, 8.71, 49.42)


class TestOverpyToGdf:
    """Building a GeoDataFrame from a parsed overpy result."""

    def test_geometry_types(self):
        """A node, a closed way and an open way give Point/Polygon/LineString."""
        gdf = overpy_to_gdf(make_result())
        assert sorted(gdf.geometry.geom_type) == ["LineString", "Point", "Polygon"]

    def test_relations_are_skipped(self):
        """The fixture's single relation contributes no row (MVP skips relations)."""
        gdf = overpy_to_gdf(make_result())
        assert len(gdf) == 3

    def test_crs_is_wgs84(self):
        """The built frame carries EPSG:4326."""
        assert str(overpy_to_gdf(make_result()).crs) == OSM_CRS

    def test_identity_and_tag_columns(self):
        """osm_id / osm_type plus each element's tags become columns."""
        gdf = overpy_to_gdf(make_result())
        assert {"osm_id", "osm_type", "amenity", "building", "highway"} <= set(
            gdf.columns
        )
        node_row = gdf[gdf.osm_type == "node"].iloc[0]
        assert node_row.osm_id == 1 and node_row.amenity == "hospital"

    def test_point_coordinates_are_lon_lat(self):
        """A node becomes Point(lon, lat)."""
        gdf = overpy_to_gdf(make_result())
        point = gdf[gdf.osm_type == "node"].geometry.iloc[0]
        assert (point.x, point.y) == (8.69, 49.41)

    def test_empty_result_is_schema_only(self):
        """A result with no nodes/ways yields a schema-only frame."""
        gdf = overpy_to_gdf(FakeResult([], [], []))
        assert len(gdf) == 0
        assert {"osm_id", "osm_type"} <= set(gdf.columns)
        assert str(gdf.crs) == OSM_CRS

    def test_short_way_is_dropped(self):
        """A way with a single coordinate is skipped (too few points)."""
        from .conftest import FakeWay

        result = FakeResult([], [FakeWay(9, {"x": "y"}, [(0.0, 0.0)])], [])
        assert len(overpy_to_gdf(result)) == 0

    def test_reserved_columns_not_clobbered_by_tags(self):
        """A tag named like a reserved column cannot overwrite identity/geometry."""
        from .conftest import FakeNode

        node = FakeNode(
            7, 49.41, 8.69, {"osm_id": "junk", "osm_type": "x", "name": "ok"}
        )
        gdf = overpy_to_gdf(FakeResult([node], [], []))
        row = gdf.iloc[0]
        assert row.osm_id == 7 and row.osm_type == "node"
        assert row.geometry.geom_type == "Point" and row["name"] == "ok"


class TestToFc:
    """Wrapping a GeoDataFrame into a FeatureCollection, normalising CRS."""

    def test_tags_crs_less_frame_to_4326(self):
        """A CRS-less frame is tagged EPSG:4326."""
        gdf = gpd.GeoDataFrame({"osm_id": [1]}, geometry=[Point(0, 0)])
        assert to_fc(gdf).crs.to_epsg() == 4326

    def test_reprojects_other_crs(self):
        """A frame in another CRS is reprojected to EPSG:4326."""
        gdf = gpd.GeoDataFrame(
            {"osm_id": [1]}, geometry=[Point(500000, 5000000)], crs="EPSG:32632"
        )
        assert to_fc(gdf).crs.to_epsg() == 4326

    def test_passthrough_4326(self):
        """An already-4326 frame keeps its CRS and rows."""
        gdf = gpd.GeoDataFrame(
            {"osm_id": [1]}, geometry=[Point(8, 49)], crs="EPSG:4326"
        )
        fc = to_fc(gdf)
        assert fc.crs.to_epsg() == 4326 and len(fc) == 1


class TestEmptyFc:
    """The schema-only empty collection."""

    def test_empty_fc_schema(self):
        """empty_fc has zero rows, the id columns, and EPSG:4326."""
        fc = empty_fc()
        assert len(fc) == 0
        assert {"osm_id", "osm_type"} <= set(fc.columns)
        assert fc.crs.to_epsg() == 4326


class TestWayGeometry:
    """The closed-ring vs line decision in _way_geometry."""

    def test_closed_ring_is_polygon(self):
        """A 4+ point ring whose ends match becomes a Polygon."""
        from earthlens.osm._helpers import _way_geometry
        from .conftest import FakeWay

        way = FakeWay(1, {}, [(0, 0), (0, 1), (1, 1), (0, 0)])
        assert isinstance(_way_geometry(way), Polygon)

    def test_open_way_is_linestring(self):
        """An open run of points becomes a LineString."""
        from earthlens.osm._helpers import _way_geometry
        from .conftest import FakeWay

        way = FakeWay(1, {}, [(0, 0), (1, 1), (2, 0)])
        assert isinstance(_way_geometry(way), LineString)

    def test_too_few_points_is_none(self):
        """A way with one point yields no geometry."""
        from earthlens.osm._helpers import _way_geometry
        from .conftest import FakeWay

        assert _way_geometry(FakeWay(1, {}, [(0, 0)])) is None
