"""Helper tests for `earthlens.drought._helpers`."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.base import SpatialExtent
from earthlens.drought._helpers import (
    EDO_ATTRIBUTION,
    SPEIBASE_ATTRIBUTION,
    USDM_ATTRIBUTION,
    attribution_for,
    bbox_from_extent,
    days_in_month,
    snap_to_cadence,
)


def test_weekly_snap_returns_previous_thursday():
    """A Tuesday-valid date snaps back to the prior Thursday release."""
    assert snap_to_cadence([dt.date(2026, 6, 23)], "weekly") == [
        dt.date(2026, 6, 18)
    ]


def test_weekly_snap_idempotent_on_thursday():
    """A Thursday snaps to itself."""
    assert snap_to_cadence([dt.date(2026, 6, 18)], "weekly") == [
        dt.date(2026, 6, 18)
    ]


def test_weekly_snap_dedupes_same_week():
    """Two dates inside one release window collapse to one fetch."""
    snapped = snap_to_cadence(
        [dt.date(2026, 6, 22), dt.date(2026, 6, 23), dt.date(2026, 6, 24)],
        "weekly",
    )
    assert snapped == [dt.date(2026, 6, 18)]


def test_weekly_snap_two_weeks_emit_two_releases():
    """A two-week span emits one Thursday per week, sorted."""
    snapped = snap_to_cadence(
        [dt.date(2026, 6, 10), dt.date(2026, 6, 23)],
        "weekly",
    )
    assert snapped == [dt.date(2026, 6, 4), dt.date(2026, 6, 18)]


@pytest.mark.parametrize(
    "day, expected_day",
    [(5, 1), (10, 1), (11, 11), (15, 11), (20, 11), (21, 21), (28, 21)],
)
def test_10day_snap_picks_dekad_start(day, expected_day):
    """The 1st / 11th / 21st are the three dekad anchors in every month."""
    snapped = snap_to_cadence([dt.date(2026, 6, day)], "10day")
    assert snapped == [dt.date(2026, 6, expected_day)]


def test_monthly_snap_returns_first_of_month():
    """Any day inside a month snaps to its first."""
    assert snap_to_cadence([dt.date(2026, 6, 25)], "monthly") == [
        dt.date(2026, 6, 1)
    ]


def test_snap_accepts_string_dates():
    """`YYYY-MM-DD` strings are accepted alongside `date` objects."""
    assert snap_to_cadence(["2026-06-25"], "monthly") == [dt.date(2026, 6, 1)]


def test_snap_accepts_datetime():
    """A `datetime` is reduced to its `date` first."""
    assert snap_to_cadence(
        [dt.datetime(2026, 6, 25, 14, 30)], "monthly"
    ) == [dt.date(2026, 6, 1)]


def test_snap_rejects_unknown_cadence():
    """Unknown cadence raises with the supported set in the message."""
    with pytest.raises(ValueError, match="unknown cadence"):
        snap_to_cadence([dt.date(2026, 6, 25)], "yearly")


def test_snap_rejects_wrong_type():
    """A bare int (not a date) trips the type guard."""
    with pytest.raises(TypeError, match="snap_to_cadence wants"):
        snap_to_cadence([12345], "monthly")  # type: ignore[list-item]


def test_bbox_from_extent_emits_west_south_east_north():
    """`SpatialExtent` projects to the `(west, south, east, north)` tuple."""
    ext = SpatialExtent(
        latitude_min=30.0,
        latitude_max=40.0,
        longitude_min=-90.0,
        longitude_max=-80.0,
    )
    assert bbox_from_extent(ext) == (-90.0, 30.0, -80.0, 40.0)


def test_attribution_for_each_transport():
    """Every known transport has a one-line attribution string."""
    assert attribution_for("usdm-geojson") == USDM_ATTRIBUTION
    assert attribution_for("edo-wcs") == EDO_ATTRIBUTION
    assert attribution_for("netcdf-url") == SPEIBASE_ATTRIBUTION


def test_attribution_for_unknown_transport_raises():
    """An unknown transport key raises KeyError (caller-supplied bug)."""
    with pytest.raises(KeyError):
        attribution_for("ftp")


def test_days_in_month_leap_year():
    """`days_in_month` honours the leap-year rule."""
    assert days_in_month(dt.date(2024, 2, 15)) == 29
    assert days_in_month(dt.date(2025, 2, 15)) == 28
    assert days_in_month(dt.date(2025, 4, 15)) == 30
