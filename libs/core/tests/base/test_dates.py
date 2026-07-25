from __future__ import annotations

import datetime as dt
import importlib

import pandas as pd
import pytest

from earthlens.base import (
    CADENCE_ALIASES,
    WHOLE_WINDOW,
    resolve_cadence,
    split_time,
    to_datetime,
)
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


class TestResolveCadence:
    """Cadence lookup that raises instead of silently substituting a default."""

    ACCEPTED = {"daily": "D", "monthly": "MS", "hourly": "h"}

    @pytest.mark.parametrize(
        "cadence,expected",
        [("daily", "D"), ("monthly", "MS"), ("hourly", "h")],
    )
    def test_accepted_cadence_returns_alias(self, cadence, expected):
        """Every accepted cadence maps to its pandas offset alias."""
        assert resolve_cadence(cadence, self.ACCEPTED) == expected

    def test_unknown_cadence_raises(self):
        """A cadence outside the accepted set raises rather than defaulting."""
        with pytest.raises(ValueError, match="is not supported"):
            resolve_cadence("yearly", self.ACCEPTED)

    def test_error_lists_accepted_spellings(self):
        """The message enumerates the accepted cadences, sorted."""
        with pytest.raises(ValueError, match=r"\['daily', 'hourly', 'monthly'\]"):
            resolve_cadence("yearly", self.ACCEPTED)

    def test_error_names_the_backend(self):
        """The backend name is quoted in the message so the user knows which failed."""
        with pytest.raises(ValueError, match="supported by CMEMS"):
            resolve_cadence("yearly", self.ACCEPTED, backend="CMEMS")

    def test_default_backend_phrase(self):
        """Without a backend name the message falls back to a generic phrase."""
        with pytest.raises(ValueError, match="supported by this backend"):
            resolve_cadence("yearly", self.ACCEPTED)

    def test_near_miss_gets_did_you_mean(self):
        """A typo close to an accepted spelling gets a did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'daily'"):
            resolve_cadence("dailyy", self.ACCEPTED)

    def test_far_miss_has_no_hint(self):
        """A cadence resembling nothing accepted gets no did-you-mean clause."""
        with pytest.raises(ValueError) as excinfo:
            resolve_cadence("zzzzzz", self.ACCEPTED)
        assert "Did you mean" not in str(excinfo.value)

    def test_empty_accepted_mapping_raises(self):
        """A backend that accepts no cadence rejects every value."""
        with pytest.raises(ValueError, match=r"Accepted: \[\]"):
            resolve_cadence("daily", {})

    def test_raises_from_none_hides_keyerror(self):
        """The ValueError is raised `from None`, so no KeyError chains onto it."""
        with pytest.raises(ValueError) as excinfo:
            resolve_cadence("yearly", self.ACCEPTED)
        assert excinfo.value.__cause__ is None

    @pytest.mark.parametrize("bad", [None, 5, ["daily"], 3.5])
    def test_non_string_cadence_raises_value_error(self, bad):
        """A non-string cadence gives a ValueError, not a difflib TypeError."""
        with pytest.raises(ValueError, match="must be a string cadence"):
            resolve_cadence(bad, self.ACCEPTED)

    def test_alias_value_passed_through_verbatim(self):
        """The mapping's value is returned as-is, not re-normalised."""
        assert resolve_cadence("weird", {"weird": "6h"}) == "6h"


class TestCadenceAliases:
    """The shared cadence vocabulary covers what the provider catalogs declare."""

    def test_covers_every_declared_cadence_literal(self):
        """Every backend's `CadenceLiteral` word resolves through the shared map.

        This is the guard that was missing: `CADENCE_ALIASES` was first derived
        from cmems alone, so 73% of eumetsat's curated rows hard-failed.
        """
        import typing

        modules = {}
        for name in ("earthdata", "eumetsat", "cmems", "drought"):
            module = importlib.import_module(f"earthlens.{name}.catalog")
            modules[name] = set(typing.get_args(module.CadenceLiteral))
        uncovered = {
            name: sorted(words - set(CADENCE_ALIASES))
            for name, words in modules.items()
            if words - set(CADENCE_ALIASES)
        }
        assert not uncovered, f"cadence words absent from CADENCE_ALIASES: {uncovered}"

    @pytest.mark.parametrize(
        "cadence,expected",
        [
            ("5min", "5min"),
            ("hourly", "h"),
            ("6hourly", "6h"),
            ("daily", "D"),
            ("8day", "8D"),
            ("10day", "10D"),
            ("16day", "16D"),
            ("weekly", "7D"),
            ("monthly", "MS"),
            ("annual", "YS"),
            ("seasonal", "QS-DEC"),
        ],
    )
    def test_periodic_words_map_to_pandas_aliases(self, cadence, expected):
        """Each periodic cadence maps to its pandas offset alias."""
        assert CADENCE_ALIASES[cadence] == expected

    @pytest.mark.parametrize(
        "cadence",
        [
            "raw",
            "native",
            "subhourly",
            "subdaily",
            "irregular",
            "climatology",
            "static",
        ],
    )
    def test_non_periodic_words_map_to_whole_window(self, cadence):
        """A cadence naming a release character resolves to the whole-window sentinel."""
        assert CADENCE_ALIASES[cadence] == WHOLE_WINDOW

    @pytest.mark.parametrize(
        "cadence", ["pentadal", "weekly", "8day", "10day", "16day"]
    )
    def test_multi_day_cadences_start_at_the_window_start(self, cadence):
        """The sliding multi-day aliases tile the window from its own first day.

        A calendar-anchored weekly alias (`W` is `W-SUN`) emits period *ends* and
        skips the window's opening days, which a per-period fetch loop would drop.
        """
        alias = CADENCE_ALIASES[cadence]
        index = pd.date_range("2024-02-01", "2024-03-19", freq=alias)
        assert index[0] == pd.Timestamp("2024-02-01")

    def test_every_periodic_alias_is_a_valid_pandas_offset(self):
        """No alias is a typo that would only fail at `date_range` time."""
        for cadence, alias in CADENCE_ALIASES.items():
            if alias == WHOLE_WINDOW:
                continue
            assert len(pd.date_range("2024-01-01", "2025-01-01", freq=alias)) > 0, (
                f"{cadence!r} maps to {alias!r}, which yields no periods"
            )
