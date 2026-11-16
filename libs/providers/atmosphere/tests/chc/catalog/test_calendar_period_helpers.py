"""Unit tests for the module-level dekad/pentad calendar helpers (#1394).

These back the `_overlapping_dates` / `_placeholders` calendar walk: resolving
a cadence to its month-reset start days, flooring a date to its containing
period, stepping to the next period across month/year boundaries, and the
inclusive period end. Pure functions over `pandas.Timestamp`, no I/O.
"""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.chc.backend import (
    _DEKAD_START_DAYS,
    _PENTAD_START_DAYS,
    _calendar_period_end,
    _calendar_period_index,
    _next_period_start,
    _period_start_days,
    _period_start_floor,
)

pytestmark = [pytest.mark.chc]


class TestPeriodStartDays:
    """`_period_start_days` maps a cadence string to its month-reset start days."""

    def test_dekadal_returns_dekad_days(self):
        """`'dekadal'` resolves to the three dekad boundaries (1, 11, 21)."""
        assert _period_start_days("dekadal") == _DEKAD_START_DAYS == (1, 11, 21)

    def test_pentadal_returns_pentad_days(self):
        """`'pentadal'` resolves to the six pentad boundaries."""
        assert (
            _period_start_days("pentadal")
            == _PENTAD_START_DAYS
            == (
                1,
                6,
                11,
                16,
                21,
                26,
            )
        )

    def test_none_returns_none(self):
        """A `None` cadence is not calendar-walked, so it returns `None`."""
        assert _period_start_days(None) is None

    @pytest.mark.parametrize(
        "cadence", ["monthly", "daily", "5-day", "10-day", "annual"]
    )
    def test_other_cadences_return_none(self, cadence):
        """Non dekad/pentad cadences (incl. GEFS 5-/10-day) return `None`."""
        assert _period_start_days(cadence) is None, (
            f"{cadence!r} should not be calendar-walked"
        )


class TestPeriodStartFloor:
    """`_period_start_floor` snaps a date back to its containing period start."""

    @pytest.mark.parametrize(
        ("day", "expected"),
        [(1, 1), (5, 1), (10, 1), (11, 11), (15, 11), (20, 11), (21, 21), (31, 21)],
    )
    def test_dekad_floor(self, day, expected):
        """Any day floors to the dekad boundary (1/11/21) at or below it."""
        out = _period_start_floor(pd.Timestamp(f"2021-01-{day:02d}"), _DEKAD_START_DAYS)
        assert out == pd.Timestamp(f"2021-01-{expected:02d}")

    @pytest.mark.parametrize(
        ("day", "expected"),
        [(1, 1), (5, 1), (6, 6), (7, 6), (25, 21), (26, 26), (28, 26)],
    )
    def test_pentad_floor(self, day, expected):
        """Any day floors to the pentad boundary at or below it."""
        out = _period_start_floor(
            pd.Timestamp(f"2021-02-{day:02d}"), _PENTAD_START_DAYS
        )
        assert out == pd.Timestamp(f"2021-02-{expected:02d}")

    def test_floor_preserves_month_and_year(self):
        """Flooring stays within the same month; only the day changes."""
        out = _period_start_floor(pd.Timestamp("2005-07-19"), _DEKAD_START_DAYS)
        assert (out.year, out.month, out.day) == (2005, 7, 11)


class TestNextPeriodStart:
    """`_next_period_start` steps to the following dekad/pentad start."""

    def test_within_month_step(self):
        """Dekad 1 -> dekad 2 stays in the month (day 1 -> day 11)."""
        out = _next_period_start(pd.Timestamp("2021-03-01"), _DEKAD_START_DAYS)
        assert out == pd.Timestamp("2021-03-11")

    def test_final_dekad_rolls_to_next_month(self):
        """Dekad 3 (day 21) rolls to day 1 of the next month."""
        out = _next_period_start(pd.Timestamp("2021-03-21"), _DEKAD_START_DAYS)
        assert out == pd.Timestamp("2021-04-01")

    def test_final_pentad_rolls_to_next_month(self):
        """Pentad 6 (day 26) rolls to day 1 of the next month."""
        out = _next_period_start(pd.Timestamp("2021-02-26"), _PENTAD_START_DAYS)
        assert out == pd.Timestamp("2021-03-01")

    def test_december_rolls_to_next_year(self):
        """The year's final dekad rolls to January 1 of the next year."""
        out = _next_period_start(pd.Timestamp("2021-12-21"), _DEKAD_START_DAYS)
        assert out == pd.Timestamp("2022-01-01")


class TestCalendarPeriodEnd:
    """`_calendar_period_end` returns the inclusive last day of a period."""

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("2020-01-01", "2020-01-10"),
            ("2020-01-11", "2020-01-20"),
            ("2020-01-21", "2020-01-31"),
            ("2020-02-21", "2020-02-29"),
            ("2021-02-21", "2021-02-28"),
            ("2021-12-21", "2021-12-31"),
        ],
    )
    def test_dekad_ends(self, start, expected):
        """Each dekad ends the day before the next start -- month-end for dekad 3."""
        out = _calendar_period_end(pd.Timestamp(start), _DEKAD_START_DAYS)
        assert out == pd.Timestamp(expected)

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("2020-01-01", "2020-01-05"),
            ("2020-01-21", "2020-01-25"),
            ("2020-01-26", "2020-01-31"),
            ("2020-02-26", "2020-02-29"),
            ("2021-02-26", "2021-02-28"),
        ],
    )
    def test_pentad_ends(self, start, expected):
        """Each pentad ends the day before the next start -- month-end for pentad 6."""
        out = _calendar_period_end(pd.Timestamp(start), _PENTAD_START_DAYS)
        assert out == pd.Timestamp(expected)


class TestCalendarPeriodIndex:
    """`_calendar_period_index` enumerates period starts across a window."""

    def test_single_period_window(self):
        """A window inside one dekad yields just that dekad's start."""
        idx = _calendar_period_index(
            pd.Timestamp("2021-01-03"), pd.Timestamp("2021-01-09"), _DEKAD_START_DAYS
        )
        assert list(idx) == [pd.Timestamp("2021-01-01")]

    def test_floors_first_period_to_contain_window_start(self):
        """A mid-period start is snapped back so the containing period is included."""
        idx = _calendar_period_index(
            pd.Timestamp("2021-06-15"), pd.Timestamp("2021-06-30"), _DEKAD_START_DAYS
        )
        assert idx[0] == pd.Timestamp("2021-06-11")

    def test_excludes_period_starting_after_window_end(self):
        """A period whose start is past `window_end` is not emitted."""
        idx = _calendar_period_index(
            pd.Timestamp("2021-01-01"), pd.Timestamp("2021-01-25"), _PENTAD_START_DAYS
        )
        assert pd.Timestamp("2021-01-26") not in idx
        assert idx[-1] == pd.Timestamp("2021-01-21")

    def test_spans_month_and_year_boundaries(self):
        """The walk crosses month and year ends without drift or gaps."""
        idx = _calendar_period_index(
            pd.Timestamp("2020-12-15"), pd.Timestamp("2021-01-15"), _DEKAD_START_DAYS
        )
        assert list(idx) == [
            pd.Timestamp("2020-12-11"),
            pd.Timestamp("2020-12-21"),
            pd.Timestamp("2021-01-01"),
            pd.Timestamp("2021-01-11"),
        ]

    def test_start_equals_end_on_boundary(self):
        """A zero-width window on a boundary yields that single period start."""
        idx = _calendar_period_index(
            pd.Timestamp("2021-05-11"), pd.Timestamp("2021-05-11"), _DEKAD_START_DAYS
        )
        assert list(idx) == [pd.Timestamp("2021-05-11")]

    def test_returns_datetime_index(self):
        """The result is a `pandas.DatetimeIndex` (same type as `date_windows`)."""
        idx = _calendar_period_index(
            pd.Timestamp("2021-01-01"), pd.Timestamp("2021-01-31"), _PENTAD_START_DAYS
        )
        assert isinstance(idx, pd.DatetimeIndex)
        assert len(idx) == 6
