"""Unit tests for `earthlens.gdacs.events` (GeoJSON -> FeatureCollection)."""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd
import pytest
from geopandas import GeoDataFrame

from earthlens.gdacs.events import (
    ATTRIBUTE_COLUMNS,
    EVENT_CRS,
    clip_to_bbox,
    empty_fc,
    geojson_to_fc,
)


@pytest.mark.gdacs
class TestGeojsonToFc:
    """`geojson_to_fc` maps a GDACS GeoJSON feed to a vector FeatureCollection."""

    def test_row_per_feature(self, make_feature: Callable[..., dict[str, Any]]):
        """One row is produced per GeoJSON feature."""
        payload = {
            "type": "FeatureCollection",
            "features": [make_feature(), make_feature(eventid=2, lon=10.0, lat=20.0)],
        }
        fc = geojson_to_fc(payload)
        assert len(fc) == 2, f"expected 2 rows, got {len(fc)}"

    def test_schema_columns_present(self, make_payload: Callable[..., dict[str, Any]]):
        """The output carries every attribute column plus geometry."""
        fc = geojson_to_fc(make_payload())
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert "geometry" in fc.columns, "geometry column must be present"

    def test_crs_is_wgs84(self, make_payload: Callable[..., dict[str, Any]]):
        """The collection is tagged EPSG:4326."""
        assert geojson_to_fc(make_payload()).crs.to_epsg() == 4326

    def test_is_geodataframe(self, make_payload: Callable[..., dict[str, Any]]):
        """The result is a GeoDataFrame subclass (pyramids FeatureCollection)."""
        assert isinstance(geojson_to_fc(make_payload()), GeoDataFrame)

    def test_event_id_is_string(self, make_payload: Callable[..., dict[str, Any]]):
        """The integer GDACS eventid is rendered as a string column."""
        fc = geojson_to_fc(make_payload())
        assert fc["event_id"].iloc[0] == "1541788"
        assert str(fc["event_id"].dtype) == "string"

    def test_severity_unpacked(self, make_payload: Callable[..., dict[str, Any]]):
        """The flat severitydata sub-dict is unpacked into three columns."""
        fc = geojson_to_fc(make_payload())
        assert float(fc["severity"].iloc[0]) == 4.7
        assert fc["severity_unit"].iloc[0] == "M"
        assert fc["severity_text"].iloc[0] == "Magnitude 4.7M"

    def test_dates_parsed_to_utc(self, make_payload: Callable[..., dict[str, Any]]):
        """from_date / to_date parse to tz-aware UTC datetimes."""
        fc = geojson_to_fc(make_payload())
        assert str(fc["from_date"].dtype) == "datetime64[ns, UTC]"
        assert fc["from_date"].iloc[0].tzname() == "UTC"

    def test_geometry_from_coordinates(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """The Point geometry is built from the feature's coordinates."""
        fc = geojson_to_fc({"features": [make_feature(lon=12.5, lat=42.0)]})
        point = fc.geometry.iloc[0]
        assert (point.x, point.y) == (12.5, 42.0), f"unexpected point {point}"

    def test_polygon_geometry_preserved(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A non-Point geometry (flood polygon) is preserved as-is."""
        polygon = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
        }
        fc = geojson_to_fc({"features": [make_feature(geometry=polygon)]})
        assert fc.geometry.iloc[0].geom_type == "Polygon"

    def test_missing_property_degrades_to_na(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A renamed/absent property degrades to a null cell, not KeyError."""
        fc = geojson_to_fc({"features": [make_feature(drop_properties=("country",))]})
        assert fc["country"].isna().iloc[0], "missing country should be NA"

    def test_missing_severity_degrades_to_na(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A feature with no severitydata yields null severity columns."""
        fc = geojson_to_fc({"features": [make_feature(drop_severity=True)]})
        assert fc["severity"].isna().iloc[0]
        assert fc["severity_unit"].isna().iloc[0]

    def test_non_numeric_alert_score_coerces_to_nan(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A non-numeric alertscore (GDACS emits "") degrades to NaN, not an error."""
        fc = geojson_to_fc({"features": [make_feature(alertscore="")]})
        assert len(fc) == 1, "the row must survive a bad alert_score"
        assert pd.isna(fc["alert_score"].iloc[0]), "bad alert_score should be NaN"

    def test_non_numeric_severity_coerces_to_nan(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A non-numeric severity value degrades to NaN rather than raising."""
        fc = geojson_to_fc({"features": [make_feature(severity="n/a")]})
        assert len(fc) == 1, "the row must survive a bad severity"
        assert pd.isna(fc["severity"].iloc[0]), "bad severity should be NaN"

    def test_geometryless_feature_has_null_geometry(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A feature with no usable geometry yields a null geometry, not an error."""
        fc = geojson_to_fc({"features": [make_feature(geometry={})]})
        assert len(fc) == 1, "the row is still emitted"
        assert fc.geometry.iloc[0] is None, "missing geometry must be null"

    def test_malformed_geometry_degrades_to_null(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A geometry shapely cannot parse degrades to null, not an error."""
        bad = {"type": "Frobnicate", "coordinates": [1, 2]}
        fc = geojson_to_fc({"features": [make_feature(geometry=bad)]})
        assert fc.geometry.iloc[0] is None, "unparseable geometry must be null"

    def test_empty_features_returns_empty_fc(self):
        """An empty features list maps to an empty FeatureCollection with the schema."""
        fc = geojson_to_fc({"type": "FeatureCollection", "features": []})
        assert len(fc) == 0, "empty feed must yield zero rows"
        assert "geometry" in fc.columns
        assert fc.crs.to_epsg() == 4326

    def test_missing_features_key_returns_empty_fc(self):
        """A payload without a 'features' key returns an empty FeatureCollection."""
        assert len(geojson_to_fc({})) == 0


@pytest.mark.gdacs
class TestEmptyFc:
    """`empty_fc` returns a schema-correct, zero-row FeatureCollection."""

    def test_zero_rows(self):
        """The empty collection has no rows."""
        assert len(empty_fc()) == 0, "empty_fc must have zero rows"

    def test_columns_and_dtypes(self):
        """Every declared column is present with its dtype."""
        fc = empty_fc()
        for column, dtype in ATTRIBUTE_COLUMNS.items():
            assert column in fc.columns, f"missing column {column!r}"
            assert (
                str(fc[column].dtype) == dtype
            ), f"{column!r} dtype {fc[column].dtype} != declared {dtype}"

    def test_crs_is_wgs84(self):
        """The empty collection is tagged EPSG:4326."""
        assert empty_fc().crs.to_epsg() == 4326


@pytest.mark.gdacs
class TestClipToBbox:
    """`clip_to_bbox` keeps only alerts intersecting the WGS84 box."""

    def test_drops_outside_events(self, make_feature: Callable[..., dict[str, Any]]):
        """An alert outside the box is dropped; one inside is kept."""
        payload = {
            "features": [
                make_feature(eventid=1, lon=12.5, lat=10.0),
                make_feature(eventid=2, lon=100.0, lat=80.0),
            ]
        }
        clipped = clip_to_bbox(geojson_to_fc(payload), [0.0, 20.0], [0.0, 20.0])
        assert len(clipped) == 1, f"expected 1 in-box alert, got {len(clipped)}"
        assert clipped["event_id"].iloc[0] == "1"

    def test_all_outside_returns_empty(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """A box matching nothing returns a schema-correct empty FC."""
        payload = {"features": [make_feature(lon=100.0, lat=80.0)]}
        clipped = clip_to_bbox(geojson_to_fc(payload), [0.0, 1.0], [0.0, 1.0])
        assert len(clipped) == 0
        assert clipped.crs.to_epsg() == 4326

    def test_empty_input_returns_empty(self):
        """Clipping an already-empty collection is a no-op empty FC."""
        assert len(clip_to_bbox(empty_fc(), [0.0, 1.0], [0.0, 1.0])) == 0

    def test_unordered_limits_handled(
        self, make_feature: Callable[..., dict[str, Any]]
    ):
        """Reversed lat/lon limits are normalised before clipping."""
        payload = {"features": [make_feature(lon=12.5, lat=10.0)]}
        clipped = clip_to_bbox(geojson_to_fc(payload), [20.0, 0.0], [20.0, 0.0])
        assert len(clipped) == 1, "reversed limits should still keep the in-box alert"


@pytest.mark.gdacs
def test_event_crs_constant():
    """The module CRS constant is WGS84."""
    assert EVENT_CRS == "EPSG:4326"
