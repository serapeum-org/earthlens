"""Helper tests for `earthlens.drought._helpers`."""

from __future__ import annotations

import datetime as dt

import pytest
from earthlens.drought._helpers import (
    EDO_ATTRIBUTION,
    SPEIBASE_ATTRIBUTION,
    USDM_ATTRIBUTION,
    attribution_for,
    bbox_from_extent,
    snap_to_cadence,
)

from earthlens.base import SpatialExtent


def test_weekly_snap_idempotent_on_released_tuesday():
    """A query at-or-after Thursday lands the same-week Tuesday composite."""
    # Friday queried on the same Friday — Tuesday's composite was released the prior day.
    assert snap_to_cadence(
        [dt.date(2026, 6, 26)], "weekly", today=dt.date(2026, 6, 26)
    ) == [dt.date(2026, 6, 23)]


def test_weekly_snap_walks_back_on_pre_release_tue_wed():
    """Tuesday and Wednesday of release week → previous week's composite."""
    # Tuesday queried on the same Tuesday — that day's JSON isn't published until Thursday.
    assert snap_to_cadence(
        [dt.date(2026, 6, 23)], "weekly", today=dt.date(2026, 6, 23)
    ) == [dt.date(2026, 6, 16)]
    # Wednesday queried on the same Wednesday — same story.
    assert snap_to_cadence(
        [dt.date(2026, 6, 24)], "weekly", today=dt.date(2026, 6, 24)
    ) == [dt.date(2026, 6, 16)]


def test_weekly_snap_walks_back_from_other_weekday():
    """Thu/Mon → most recent released Tuesday composite."""
    for queried, expected in (
        (dt.date(2026, 6, 25), dt.date(2026, 6, 23)),  # Thu → same-week Tue
        (dt.date(2026, 6, 22), dt.date(2026, 6, 16)),  # Mon → prior Tue
    ):
        assert snap_to_cadence([queried], "weekly", today=queried) == [expected]


def test_weekly_snap_does_not_overshoot_historical_query():
    """A historical Tuesday queried much later stays at itself (not walked back).

    Round 2's G5: F5's walk-back rule originally triggered whenever the
    input date's weekday was Tue/Wed, regardless of `today`. A query for
    `2026-06-23` made in 2027 would silently fetch the 06-16 composite
    instead of the (long-released) 06-23 one.
    """
    assert snap_to_cadence(
        [dt.date(2026, 6, 23)],  # a Tuesday
        "weekly",
        today=dt.date(2027, 1, 1),  # ~6 months later, well past release Thursday
    ) == [dt.date(2026, 6, 23)]


def test_weekly_snap_dedupes_same_week():
    """Multiple released-side dates inside one window collapse to one fetch."""
    snapped = snap_to_cadence(
        [dt.date(2026, 6, 25), dt.date(2026, 6, 26), dt.date(2026, 6, 27)],
        "weekly",
        today=dt.date(2026, 6, 27),
    )
    assert snapped == [dt.date(2026, 6, 23)]


def test_weekly_snap_two_weeks_emit_two_releases():
    """A two-week span emits one Tuesday per week, sorted."""
    # Both Tuesdays' release Thursdays (06-04, 06-25) are past 2026-06-26,
    # so each Tuesday lands on itself — no walk-back.
    snapped = snap_to_cadence(
        [dt.date(2026, 6, 2), dt.date(2026, 6, 26)],
        "weekly",
        today=dt.date(2026, 6, 26),
    )
    assert snapped == [dt.date(2026, 6, 2), dt.date(2026, 6, 23)]


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
    assert snap_to_cadence([dt.date(2026, 6, 25)], "monthly") == [dt.date(2026, 6, 1)]


def test_snap_accepts_string_dates():
    """`YYYY-MM-DD` strings are accepted alongside `date` objects."""
    assert snap_to_cadence(["2026-06-25"], "monthly") == [dt.date(2026, 6, 1)]


def test_snap_accepts_datetime():
    """A `datetime` is reduced to its `date` first."""
    assert snap_to_cadence([dt.datetime(2026, 6, 25, 14, 30)], "monthly") == [
        dt.date(2026, 6, 1)
    ]


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
