"""Tests for the `CadenceBucketer` and `CompositeEngine` in `gee._composite`."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.base import date_windows
from earthlens.gee._composite import Bucket, CadenceBucketer, CompositeEngine
from earthlens.gee.reducers import temporal_reducer_for
from earthlens.gee.work import RasterWorkUnit


class _FakeImage:
    """Stands in for the reduced `ee.Image`, remembering which collection made it."""

    def __init__(self, collection):
        self.collection = collection


class _FakeCollection:
    """Records the `filterDate` window each bucket requested, then reduces."""

    def __init__(self, window=None):
        self.window = window  # (start_str, end_str) for a filtered bucket, else None

    def filterDate(self, start, end):  # noqa: N802 - mirrors the ee API
        return _FakeCollection(window=(start, end))

    def mean(self):
        return _FakeImage(self)


def _pre_refactor_composite(collection, reducer, start, end, freq, *, whole):
    """Replay the old procedural `_composite` to compare the refactor against."""
    units = []
    if whole:
        units.append(RasterWorkUnit(reducer(collection), start, start, end))
        return units
    starts = date_windows(start, end, freq, inclusive="left")
    for i, bucket_start in enumerate(starts):
        bucket_end = starts[i + 1] if i + 1 < len(starts) else pd.Timestamp(end)
        window = collection.filterDate(
            bucket_start.strftime("%Y-%m-%d"), bucket_end.strftime("%Y-%m-%d")
        )
        units.append(
            RasterWorkUnit(
                reducer(window),
                bucket_start.to_pydatetime(),
                bucket_start.to_pydatetime(),
                bucket_end.to_pydatetime(),
            )
        )
    return units


def _as_rows(units):
    """Reduce each unit to the comparable `(when, filter-window, start, end)`."""
    rows = []
    for when, image, bucket_start, bucket_end in units:
        rows.append((when, image.collection.window, bucket_start, bucket_end))
    return rows


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


def _engine(cadence):
    """A mean-reducing `CompositeEngine` at the given cadence."""
    return CompositeEngine(
        reducer=temporal_reducer_for("mean"), cadence=CadenceBucketer(cadence)
    )


class TestCompositeEngineParity:
    """`composite_ee` yields byte-for-byte the pre-refactor four-tuple sequence."""

    @pytest.mark.parametrize(
        ("cadence", "freq", "start", "end"),
        [
            ("daily", "D", dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 3)),
            ("monthly", "MS", dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)),
            ("yearly", "YS", dt.datetime(2020, 1, 1), dt.datetime(2022, 1, 1)),
        ],
    )
    def test_periodic_matches_the_old_split(self, cadence, freq, start, end):
        """Each periodic cadence splits identically to the old `date_windows` loop."""
        engine = _engine(cadence)
        got = list(
            engine.composite_ee(_FakeCollection(), start, end, is_image_collection=True)
        )
        expected = _pre_refactor_composite(
            _FakeCollection(),
            temporal_reducer_for("mean"),
            start,
            end,
            freq,
            whole=False,
        )
        assert _as_rows(got) == _as_rows(expected)

    def test_raw_is_one_whole_window_bucket(self):
        """`raw` reduces the whole collection once, with no `filterDate`."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 30)
        got = list(
            _engine("raw").composite_ee(
                _FakeCollection(), start, end, is_image_collection=True
            )
        )
        assert len(got) == 1
        when, image, bucket_start, bucket_end = got[0]
        assert image.collection.window is None  # the whole collection, unfiltered
        assert (when, bucket_start, bucket_end) == (start, start, end)

    def test_static_image_is_one_bucket_regardless_of_cadence(self):
        """A static (non-collection) dataset is one whole-window bucket even monthly."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)
        got = list(
            _engine("monthly").composite_ee(
                _FakeCollection(), start, end, is_image_collection=False
            )
        )
        assert len(got) == 1
        assert got[0].image.collection.window is None

    def test_yields_raster_work_units(self):
        """Each yielded item is a `RasterWorkUnit`, so the download seam is unchanged."""
        got = list(
            _engine("monthly").composite_ee(
                _FakeCollection(),
                dt.datetime(2020, 6, 1),
                dt.datetime(2020, 8, 1),
                is_image_collection=True,
            )
        )
        assert all(isinstance(unit, RasterWorkUnit) for unit in got)
        assert [unit.kind for unit in got] == ["raster", "raster"]

    def test_last_bucket_end_is_pinned_to_the_request_end(self):
        """The final bucket's exclusive end is the request end, not the next period."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 15)
        got = list(
            _engine("monthly").composite_ee(
                _FakeCollection(), start, end, is_image_collection=True
            )
        )
        assert got[-1].bucket_end == end


class TestBuckets:
    """`buckets` yields the half-open windows directly."""

    def test_periodic_buckets_cover_the_window(self):
        """Monthly buckets tile the window, last end pinned to the request end."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 8, 1)
        got = list(_engine("monthly").buckets(start, end, is_image_collection=True))
        assert got == [
            Bucket(dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)),
            Bucket(dt.datetime(2020, 7, 1), dt.datetime(2020, 8, 1)),
        ]

    def test_whole_window_is_a_single_bucket(self):
        """A raw request is one `[start, end)` bucket."""
        start, end = dt.datetime(2020, 6, 1), dt.datetime(2020, 6, 30)
        got = list(_engine("raw").buckets(start, end, is_image_collection=True))
        assert got == [Bucket(start, end)]
