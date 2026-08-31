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
    ohsome_body_preview,
    ohsome_error_response,
    ohsome_http_status,
    ohsome_response_is_non_json,
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


class TestOhsomeHttpStatus:
    """Recovering the HTTP status behind an ohsome SDK failure."""

    def test_reads_error_code_directly(self):
        """An OhsomeException-like error exposes its error_code."""

        class _Err(Exception):
            error_code = 429

        assert ohsome_http_status(_Err()) == 429

    def test_reads_response_status_code(self):
        """An error carrying a response object yields its status_code."""
        import types

        exc = RuntimeError("boom")
        exc.response = types.SimpleNamespace(status_code=503)
        assert ohsome_http_status(exc) == 503

    def test_walks_context_chain_to_http_error(self):
        """A leaked JSONDecodeError exposes the 403 via its __context__ chain."""
        import types

        http_error = RuntimeError("403 Forbidden")
        http_error.response = types.SimpleNamespace(status_code=403)
        leaked = ValueError("Expecting value")
        leaked.__context__ = http_error
        assert ohsome_http_status(leaked) == 403

    def test_returns_none_without_a_status(self):
        """A plain error with no status anywhere yields None."""
        assert ohsome_http_status(RuntimeError("no status here")) is None

    def test_bool_error_code_is_not_a_status(self):
        """A bool error_code is rejected (bool is an int subclass), yielding None."""
        exc = RuntimeError("weird")
        exc.error_code = True
        assert ohsome_http_status(exc) is None

    def test_survives_a_cyclic_chain(self):
        """A self-referential __context__ cycle terminates instead of looping."""
        first = RuntimeError("a")
        second = RuntimeError("b")
        first.__context__ = second
        second.__context__ = first
        assert ohsome_http_status(first) is None


class TestOhsomeResponseRecovery:
    """Recovering the response, non-JSON verdict, and body preview (#930)."""

    def test_error_response_from_chain(self):
        """The response object is dug out of the __context__ chain."""
        import types

        response = types.SimpleNamespace(status_code=200, headers={}, text="x")
        http_error = RuntimeError("boom")
        http_error.response = response
        leaked = ValueError("Expecting value")
        leaked.__context__ = http_error
        assert ohsome_error_response(leaked) is response

    def test_error_response_none_when_absent(self):
        """No response anywhere yields None."""
        assert ohsome_error_response(RuntimeError("no response")) is None

    def test_error_response_ignores_response_without_status(self):
        """A response object with no integer status_code is not returned."""
        import types

        exc = RuntimeError("x")
        exc.response = types.SimpleNamespace(status_code=None)
        assert ohsome_error_response(exc) is None

    def test_non_json_detects_stdlib_json_decode_error(self):
        """A stdlib json.JSONDecodeError anywhere in the chain reads as non-JSON."""
        import json

        outer = RuntimeError("wrapped")
        outer.__context__ = json.JSONDecodeError("Expecting value", "<html>", 0)
        assert ohsome_response_is_non_json(outer) is True

    def test_non_json_detects_by_class_name(self):
        """A JSONDecodeError variant (by class name) also reads as non-JSON."""

        class JSONDecodeError(ValueError):
            pass

        assert ohsome_response_is_non_json(JSONDecodeError("bad")) is True

    def test_non_json_false_for_plain_error(self):
        """A plain error (a JSON error served as JSON) is not a non-JSON body."""
        assert ohsome_response_is_non_json(RuntimeError("bad request")) is False

    def test_non_json_false_for_non_valueerror_named_class(self):
        """A class named JSONDecodeError that is not a ValueError is excluded."""

        class JSONDecodeError(RuntimeError):
            pass

        assert ohsome_response_is_non_json(JSONDecodeError("x")) is False

    def test_body_preview_truncates(self):
        """The body preview is truncated to the requested limit."""
        import types

        response = types.SimpleNamespace(text="abcdefghij")
        assert ohsome_body_preview(response, limit=4) == "abcd"

    def test_body_preview_none_for_none(self):
        """No response yields no preview."""
        assert ohsome_body_preview(None) is None

    def test_body_preview_none_when_text_unreadable(self):
        """A body that cannot be decoded yields None rather than raising."""

        class _BadBody:
            @property
            def text(self):
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "boom")

        assert ohsome_body_preview(_BadBody()) is None


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
