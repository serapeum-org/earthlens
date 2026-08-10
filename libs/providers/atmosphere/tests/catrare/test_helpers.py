"""Unit tests for `earthlens.catrare._helpers`."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import geopandas as gpd
import pytest
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import box

from earthlens.catrare._helpers import (
    _is_global,
    build_feature_collection,
    filter_events,
)

pytestmark = pytest.mark.catrare


def _events() -> FeatureCollection:
    """A three-event collection (EPSG:4326) with dates spanning 2005 and 2021."""
    gdf = gpd.GeoDataFrame(
        {
            "Event_ID": [1, 2, 3],
            "Date_START": ["2021-07-14 09:50:00", "2021-07-15 00:50:00", "2005-08-01 00:50:00"],
            "Date_END": ["2021-07-14 12:50:00", "2021-07-15 06:50:00", "2005-08-01 06:50:00"],
            "Area": [10.0, 20.0, 30.0],
        },
        geometry=[box(6, 50, 7, 51), box(7, 51, 8, 52), box(13, 48, 14, 49)],
        crs="EPSG:4326",
    )
    return FeatureCollection(gdf)


def _global_space() -> SimpleNamespace:
    """A whole-globe spatial extent (no bbox filter)."""
    return SimpleNamespace(
        latitude_min=-90.0, latitude_max=90.0, longitude_min=-180.0, longitude_max=180.0
    )


def test_build_feature_collection_selects_columns():
    """The trimmed collection keeps the requested event columns + geometry."""
    trimmed = build_feature_collection(_events(), ["Event_ID", "Area"])
    assert set(trimmed.columns) == {"Event_ID", "Area", "geometry"}


def test_build_feature_collection_missing_column_raises():
    """A missing event column is a clean domain error, not a KeyError."""
    with pytest.raises(ValueError, match="missing expected column"):
        build_feature_collection(_events(), ["Event_ID", "Ghost"])


def test_filter_events_by_date_window():
    """The date window keeps only events overlapping it."""
    result = filter_events(
        _events(), _global_space(), datetime(2021, 7, 1), datetime(2021, 7, 31)
    )
    assert sorted(result["Event_ID"]) == [1, 2]


def test_filter_events_open_ended_start():
    """A start-only window drops events that end before it."""
    result = filter_events(_events(), _global_space(), datetime(2010, 1, 1), None)
    assert sorted(result["Event_ID"]) == [1, 2]


def test_filter_events_by_bbox():
    """A bbox keeps only events whose geometry intersects it."""
    space = SimpleNamespace(
        latitude_min=49.5, latitude_max=51.5, longitude_min=5.5, longitude_max=7.5
    )
    result = filter_events(_events(), space, None, None)
    assert sorted(result["Event_ID"]) == [1, 2]


def test_filter_events_no_filters_keeps_all():
    """With no date or bbox filter every event is returned."""
    result = filter_events(_events(), _global_space(), None, None)
    assert len(result) == 3


def test_is_global_true_for_whole_earth():
    """`_is_global` is True only for a full WGS84 box."""
    assert _is_global(_global_space())
    assert not _is_global(
        SimpleNamespace(
            latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
        )
    )
