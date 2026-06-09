from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.base import to_datetime


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
