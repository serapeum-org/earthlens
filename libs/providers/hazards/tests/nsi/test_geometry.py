"""Unit tests for the NSI geometry helpers."""

from __future__ import annotations

import json

import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.nsi.geometry import (
    arcgis_envelope,
    bbox_from_limits,
    nsi_polygon_body,
    to_feature_collection,
)

pytestmark = pytest.mark.nsi


@pytest.mark.unit
class TestBboxFromLimits:
    """`bbox_from_limits` ordering and validation."""

    def test_returns_xmin_ymin_xmax_ymax(self) -> None:
        """The box is returned lon-first, lat-second."""
        assert bbox_from_limits([29.95, 29.96], [-90.07, -90.06]) == (
            -90.07,
            29.95,
            -90.06,
            29.96,
        )

    @pytest.mark.parametrize(
        "lat_lim,lon_lim",
        [([29.95], [-90.07, -90.06]), (None, [-90.07, -90.06]), ([29.95, 29.96], [])],
    )
    def test_bad_shape_raises(self, lat_lim, lon_lim) -> None:
        """A non-two-element limit is rejected."""
        with pytest.raises(ValueError):
            bbox_from_limits(lat_lim, lon_lim)

    def test_inverted_bound_raises(self) -> None:
        """An inverted bound (min > max) is rejected."""
        with pytest.raises(ValueError):
            bbox_from_limits([30.0, 29.0], [-90.07, -90.06])

    def test_degenerate_point_box_allowed(self) -> None:
        """A zero-width axis (min == max) is allowed, matching SpatialExtent."""
        assert bbox_from_limits([30.0, 30.0], [-90.0, -90.0]) == (
            -90.0,
            30.0,
            -90.0,
            30.0,
        )


@pytest.mark.unit
class TestNsiPolygonBody:
    """`nsi_polygon_body` builds a closed rectangle FeatureCollection body."""

    def test_closed_ring_of_five_points(self) -> None:
        """The polygon ring closes (first == last) with five vertices."""
        body = nsi_polygon_body([29.95, 29.96], [-90.07, -90.06])
        ring = body["features"][0]["geometry"]["coordinates"][0]
        assert len(ring) == 5
        assert ring[0] == ring[-1]

    def test_is_a_feature_collection(self) -> None:
        """The body is a GeoJSON FeatureCollection with one polygon feature."""
        body = nsi_polygon_body([29.95, 29.96], [-90.07, -90.06])
        assert body["type"] == "FeatureCollection"
        assert body["features"][0]["geometry"]["type"] == "Polygon"


@pytest.mark.unit
class TestArcgisEnvelope:
    """`arcgis_envelope` emits the esri query params."""

    def test_envelope_and_geojson_format(self) -> None:
        """The params carry a WGS84 envelope and request GeoJSON output."""
        params = arcgis_envelope([29.95, 29.96], [-90.07, -90.06])
        assert params["geometryType"] == "esriGeometryEnvelope"
        assert params["f"] == "geojson"
        assert json.loads(params["geometry"]) == {
            "xmin": -90.07,
            "ymin": 29.95,
            "xmax": -90.06,
            "ymax": 29.96,
        }

    def test_out_fields_override(self) -> None:
        """A custom `out_fields` is passed through."""
        params = arcgis_envelope(
            [29.95, 29.96], [-90.07, -90.06], out_fields="FLD_ZONE"
        )
        assert params["outFields"] == "FLD_ZONE"


@pytest.mark.unit
class TestToFeatureCollection:
    """`to_feature_collection` wraps GeoJSON into a pyramids collection."""

    def test_wraps_features(self) -> None:
        """A populated GeoJSON becomes a non-empty FeatureCollection."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-90.0, 29.9]},
                    "properties": {"a": 1},
                }
            ],
        }
        fc = to_feature_collection(geojson)
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 1

    def test_empty_features_is_empty_collection(self) -> None:
        """An empty features list yields an empty collection, not an error."""
        fc = to_feature_collection({"type": "FeatureCollection", "features": []})
        assert isinstance(fc, FeatureCollection)
        assert len(fc) == 0

    def test_missing_features_key_raises(self) -> None:
        """A mapping without a `features` key is rejected."""
        with pytest.raises(ValueError):
            to_feature_collection({"type": "Point"})
