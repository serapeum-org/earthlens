from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.base import split_time, to_datetime
from earthlens.base._dates import _strip_tz


class TestToDatetime:
    """Flexible coercion of date-like values into datetime."""

    def test_iso_string_with_matching_fmt(self):
        """An ISO date string parses with the default format."""
        assert to_datetime("2022-01-01", fmt="%Y-%m-%d") == dt.datetime(2022, 1, 1)

    def test_string_without_fmt_parses_iso(self):
        """Without a fmt, a string is parsed leniently as ISO-8601."""
        assert to_datetime("2022-01-01") == dt.datetime(2022, 1, 1)

    def test_full_iso_string_falls_back_when_fmt_mismatches(self):
        """A timestamp string parses even when fmt is a plain date format."""
        assert to_datetime("2022-01-01T06:30", fmt="%Y-%m-%d") == dt.datetime(
            2022, 1, 1, 6, 30
        )

    def test_custom_fmt(self):
        """A non-ISO string parses with an explicit fmt."""
        assert to_datetime("01/02/2022", fmt="%d/%m/%Y") == dt.datetime(2022, 2, 1)

    def test_date_is_promoted_to_midnight(self):
        """A date is promoted to midnight of that day."""
        assert to_datetime(dt.date(2022, 1, 1)) == dt.datetime(2022, 1, 1, 0, 0)

    def test_datetime_passes_through(self):
        """A datetime is returned unchanged."""
        value = dt.datetime(2022, 1, 1, 9, 15)
        assert to_datetime(value) is value

    def test_pandas_timestamp_passes_through(self):
        """A pandas Timestamp (a datetime subclass) is returned unchanged."""
        value = pd.Timestamp("2022-01-01T12:00")
        assert to_datetime(value) is value

    def test_unsupported_type_raises(self):
        """A non-date value is rejected with a clear message."""
        with pytest.raises(TypeError, match="must be a datetime, date, or string"):
            to_datetime(20220101)

    def test_unparseable_string_raises(self):
        """A string that is neither fmt- nor ISO-parseable raises."""
        with pytest.raises(ValueError):
            to_datetime("not-a-date", fmt="%Y-%m-%d")

    def test_aware_string_normalized_to_naive_utc(self):
        """An offset-bearing string becomes naive UTC (+02:00 06:30 -> 04:30)."""
        result = to_datetime("2022-01-01T06:30:00+02:00")
        assert result == dt.datetime(2022, 1, 1, 4, 30)
        assert result.tzinfo is None

    def test_aware_datetime_normalized_to_naive_utc(self):
        """An aware datetime is converted to UTC and stripped of tzinfo."""
        aware = dt.datetime(
            2022, 1, 1, 6, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))
        )
        result = to_datetime(aware)
        assert result == dt.datetime(2022, 1, 1, 4, 30)
        assert result.tzinfo is None

    def test_strptime_with_tz_offset_normalized(self):
        """A fmt carrying %z still yields a naive UTC datetime."""
        result = to_datetime("2022-01-01T06:30+0200", fmt="%Y-%m-%dT%H:%M%z")
        assert result == dt.datetime(2022, 1, 1, 4, 30)
        assert result.tzinfo is None

    def test_mixed_naive_and_aware_inputs_are_comparable(self):
        """A naive start and an aware end no longer raise on comparison (M1)."""
        start = to_datetime("2022-01-01")
        end = to_datetime("2022-01-02T00:00:00+05:00")
        assert start < end  # would raise naive-vs-aware TypeError before the fix


class TestStripTz:
    """Naive-UTC normalization helper backing to_datetime."""

    def test_naive_returned_unchanged_identity(self):
        """A naive datetime is returned as the same object."""
        naive = dt.datetime(2022, 1, 1, 9, 15)
        assert _strip_tz(naive) is naive

    def test_aware_converted_to_utc_and_stripped(self):
        """An aware datetime is shifted to UTC and loses its tzinfo."""
        aware = dt.datetime(
            2022, 1, 1, 9, 15, tzinfo=dt.timezone(dt.timedelta(hours=-3))
        )
        result = _strip_tz(aware)
        assert result == dt.datetime(2022, 1, 1, 12, 15)
        assert result.tzinfo is None


class TestSplitTime:
    """Splitting a single time= range into a (start, end) pair."""

    def test_interval_string(self):
        """A STAC-style 'a/b' string splits on the slash."""
        assert split_time("2020-01-01/2020-01-31") == ("2020-01-01", "2020-01-31")

    def test_open_ended_interval(self):
        """An empty half of the interval becomes None (open-ended)."""
        assert split_time("2020-01-01/") == ("2020-01-01", None)
        assert split_time("/2020-01-31") == (None, "2020-01-31")

    def test_two_sequence(self):
        """A (start, end) tuple / list is returned verbatim."""
        assert split_time(("2020-01-01", "2020-02-01")) == ("2020-01-01", "2020-02-01")
        assert split_time(["a", "b"]) == ("a", "b")

    def test_slice(self):
        """A slice maps start/stop to (start, end)."""
        assert split_time(slice("2020-01-01", "2020-03-01")) == (
            "2020-01-01",
            "2020-03-01",
        )

    def test_slice_with_step_raises(self):
        """A slice carrying a step is rejected (no meaning on a date range)."""
        with pytest.raises(ValueError, match="slice does not accept a step"):
            split_time(slice("2020-01-01", "2020-03-01", 2))

    def test_single_string_is_an_instant(self):
        """A plain date string with no slash is a single instant."""
        assert split_time("2020-01-01") == ("2020-01-01", "2020-01-01")

    def test_single_date_object_is_an_instant(self):
        """A date / datetime object is a single instant."""
        day = dt.date(2020, 1, 1)
        assert split_time(day) == (day, day)

    def test_wrong_length_sequence_raises(self):
        """A sequence that is not exactly two elements is rejected."""
        with pytest.raises(ValueError, match="2 elements"):
            split_time(("2020-01-01", "2020-02-01", "2020-03-01"))

    def test_unsupported_type_raises(self):
        """A non-range value is rejected with a clear message."""
        with pytest.raises(TypeError, match="time= must be"):
            split_time(2020)
