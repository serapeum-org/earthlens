"""Tests for the pure Argo request helpers (no argopy, no network)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.argo import _helpers
from earthlens.argo._helpers import Selection, empty_canonical, parse_selection, region_box
from earthlens.base import SpatialExtent, TemporalExtent

pytestmark = pytest.mark.argo


def _space() -> SpatialExtent:
    """A north-Atlantic test bbox."""
    return SpatialExtent.from_pairs(lat_lim=[40.0, 45.0], lon_lim=[-60.0, -55.0])


def _time() -> TemporalExtent:
    """A two-week January 2020 window."""
    start = dt.datetime(2020, 1, 1)
    end = dt.datetime(2020, 1, 15)
    return TemporalExtent(
        start_date=start,
        end_date=end,
        resolution="profile",
        dates=pd.DatetimeIndex([start, end]),
    )


def test_parse_selection_region_for_parameters():
    """A parameter list (or empty list) is a region selection."""
    assert parse_selection(["TEMP", "PSAL"]) == Selection("region")
    assert parse_selection([]) == Selection("region")


def test_parse_selection_single_float():
    """A float: token parses one WMO id."""
    assert parse_selection(["float:6902746"]) == Selection("float", (6902746,))


def test_parse_selection_multi_float():
    """A comma-separated float: token parses every WMO id."""
    assert parse_selection(["float:6902746,6902747"]) == Selection(
        "float", (6902746, 6902747)
    )


def test_parse_selection_profile():
    """A profile: token parses the WMO id and cycle number."""
    assert parse_selection(["profile:6902746/12"]) == Selection(
        "profile", (6902746,), 12
    )


def test_parse_selection_rejects_mixed():
    """A selector mixed with other entries is rejected."""
    with pytest.raises(ValueError, match="only entry"):
        parse_selection(["float:6902746", "TEMP"])


def test_parse_selection_rejects_bad_profile():
    """A profile: token without a cycle is rejected."""
    with pytest.raises(ValueError, match="profile:<WMO>/<cycle>"):
        parse_selection(["profile:6902746"])


def test_parse_selection_rejects_non_numeric_float():
    """A non-numeric WMO id raises a friendly error, not a raw int() error."""
    with pytest.raises(ValueError, match="non-numeric WMO id"):
        parse_selection(["float:abc"])


def test_parse_selection_rejects_non_numeric_cycle():
    """A non-numeric cycle raises a friendly error naming the token."""
    with pytest.raises(ValueError, match="non-numeric cycle"):
        parse_selection(["profile:6902746/xx"])


def test_parse_selection_rejects_empty_float():
    """A bare float: token with no WMO id is rejected."""
    with pytest.raises(ValueError, match="no WMO id"):
        parse_selection(["float:"])


def test_region_box_order():
    """region_box emits the A1-pinned [W, E, S, N, dmin, dmax, start, end] order."""
    box = region_box(_space(), _time(), (0.0, 2000.0))
    assert box == [
        -60.0,
        -55.0,
        40.0,
        45.0,
        0.0,
        2000.0,
        "2020-01-01T00:00:00",
        "2020-01-15T00:00:00",
    ]


def test_empty_canonical_default_columns():
    """The default empty frame carries the canonical Argo columns, zero rows."""
    df = empty_canonical()
    assert list(df.columns) == _helpers.ARGO_COLUMNS
    assert len(df) == 0


def test_empty_canonical_custom_columns():
    """An explicit column list is honoured."""
    df = empty_canonical(["TIME", "TEMP"])
    assert list(df.columns) == ["TIME", "TEMP"]
    assert len(df) == 0
