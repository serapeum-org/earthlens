"""Property-based tests for `earthlens.base.date_windows` and `split_time`.

`date_windows` must return contiguous, evenly `freq`-spaced, in-range period
starts for every cadence alias; `split_time` must round-trip against a `/` join
and preserve two-element and instant inputs.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from earthlens.base import date_windows, split_time

# A spread of fixed and calendar cadence aliases (the wider alias surface).
_FREQS = st.sampled_from(["h", "6h", "12h", "D", "2D", "W", "MS", "QS", "YS"])
# Bounded so no single index blows up (hourly x 120 days = 2880 rows at most).
_DATES = st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2030, 12, 31))
_SPANS = st.integers(min_value=0, max_value=120)

# A safe token alphabet for split_time strings: no "/" (the delimiter) and no
# surrounding whitespace (split_time strips each half), so the round-trip is exact.
_TOKENS = st.text(alphabet="ABCabc0123:-T.", min_size=1, max_size=12)


@pytest.mark.unit
class TestDateWindowsProperties:
    """date_windows yields contiguous, in-range period starts for any cadence."""

    @given(start=_DATES, span=_SPANS, freq=_FREQS)
    def test_windows_are_ordered_unique_and_evenly_spaced(self, start, span, freq):
        """Every cadence gives a strictly increasing, evenly freq-spaced index."""
        end = start + dt.timedelta(days=span)
        idx = date_windows(start, end, freq)
        assert idx.is_monotonic_increasing, idx
        assert idx.is_unique, idx
        # Contiguity checked independently of pd.date_range: each consecutive
        # pair differs by exactly one freq offset, so there is no gap or overlap
        # between windows.
        offset = pd.tseries.frequencies.to_offset(freq)
        for prev, cur in zip(idx[:-1], idx[1:]):
            assert cur == prev + offset, (prev, cur, freq)

    @given(start=_DATES, span=_SPANS, freq=_FREQS)
    def test_windows_stay_within_the_requested_span(self, start, span, freq):
        """No window falls outside [start, end], and none is omitted at the edges."""
        end = start + dt.timedelta(days=span)
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        idx = date_windows(start, end, freq)
        if not len(idx):
            return
        offset = pd.tseries.frequencies.to_offset(freq)
        assert idx[0] >= start_ts, (idx[0], start_ts)
        assert idx[-1] <= end_ts, (idx[-1], end_ts)
        assert idx[0] - offset < start_ts, "an earlier window was omitted"
        assert idx[-1] + offset > end_ts, "a trailing window was omitted"

    @given(start=_DATES, span=_SPANS, freq=_FREQS)
    def test_stricter_inclusive_modes_are_subsets_of_both(self, start, span, freq):
        """left / right / neither only ever drop endpoints from the both set."""
        end = start + dt.timedelta(days=span)
        both = set(date_windows(start, end, freq, inclusive="both"))
        for mode in ("left", "right", "neither"):
            subset = set(date_windows(start, end, freq, inclusive=mode))
            assert subset <= both, mode


@pytest.mark.unit
class TestSplitTimeProperties:
    """split_time preserves halves across every accepted form."""

    @given(start=_TOKENS, end=_TOKENS)
    def test_slash_string_round_trips(self, start, end):
        """`split_time(a + "/" + b)` inverts the `/` join into `(a, b)`."""
        assert split_time(f"{start}/{end}") == (start, end)

    @given(start=_TOKENS, end=_TOKENS)
    def test_two_sequence_is_returned_verbatim(self, start, end):
        """A `[start, end]` list or tuple is returned as `(start, end)`."""
        assert split_time([start, end]) == (start, end)
        assert split_time((start, end)) == (start, end)

    @given(value=_TOKENS)
    def test_single_token_is_an_instant(self, value):
        """A string with no `/` is an instant returned as `(value, value)`."""
        assert split_time(value) == (value, value)

    @given(token=_TOKENS)
    def test_open_ended_halves_become_none(self, token):
        """An empty half of a `/` string becomes `None` (open-ended)."""
        assert split_time(f"{token}/") == (token, None)
        assert split_time(f"/{token}") == (None, token)

    @given(size=st.sampled_from([0, 1, 3, 4, 5]))
    def test_wrong_length_sequence_raises(self, size):
        """A sequence that is not exactly two elements is rejected."""
        with pytest.raises(ValueError, match="2 elements"):
            split_time([0] * size)

    @given(start=_TOKENS, end=_TOKENS, step=st.integers(min_value=1, max_value=9))
    def test_slice_round_trips_and_a_step_is_rejected(self, start, end, step):
        """A stepless slice gives `(start, stop)`; a step has no meaning and raises."""
        assert split_time(slice(start, end)) == (start, end)
        stepped = slice(start, end, step)
        with pytest.raises(ValueError, match="step"):
            split_time(stepped)

    @given(instant=st.one_of(st.dates(), st.datetimes()))
    def test_date_or_datetime_is_an_instant(self, instant):
        """A bare date / datetime value is an instant returned as `(value, value)`."""
        assert split_time(instant) == (instant, instant)

    @given(
        bad=st.one_of(
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
        )
    )
    def test_unsupported_type_raises_type_error(self, bad):
        """A value that is not a string / pair / slice / date is rejected."""
        with pytest.raises(TypeError):
            split_time(bad)
