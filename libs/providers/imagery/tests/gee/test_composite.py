"""Tests for the `CadenceBucketer` and `CompositeEngine` in `gee._composite`."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.base import date_windows
from earthlens.gee._composite import CadenceBucketer


class TestCadenceBucketer:
    """`CadenceBucketer` resolves a cadence to a pandas freq and its windows."""

    @pytest.mark.parametrize(
        ("cadence", "freq"),
        [("daily", "D"), ("monthly", "MS"), ("yearly", "YS")],
    )
    def test_periodic_cadence_resolves_to_its_freq(self, cadence, freq):
        """Each periodic cadence maps to its pandas offset alias."""
        bucketer = CadenceBucketer(cadence)
        assert bucketer.freq == freq
        assert bucketer.cadence == cadence
        assert bucketer.is_whole_window is False

    def test_raw_is_whole_window(self):
        """`raw` is the single whole-window bucket, not a period split."""
        bucketer = CadenceBucketer("raw")
        assert bucketer.is_whole_window is True

    def test_windows_match_date_windows_left_closed(self):
        """The bucket edges are exactly `date_windows(..., inclusive='left')`."""
        bucketer = CadenceBucketer("monthly")
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)
        assert list(bucketer.windows(start, end)) == list(
            date_windows(start, end, "MS", inclusive="left")
        )

    def test_unknown_cadence_raises_with_a_suggestion(self):
        """A mistyped cadence is refused with a did-you-mean, not defaulted."""
        with pytest.raises(ValueError, match="Did you mean 'monthly'"):
            CadenceBucketer("monthy")

    def test_unknown_cadence_names_the_backend(self):
        """The error names GEE, since the accepted set is GEE's."""
        with pytest.raises(ValueError, match="not supported by GEE"):
            CadenceBucketer("weekly")
