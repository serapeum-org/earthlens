"""Tests for `GapFill` — temporal interpolation before reduction."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import gapfill as gapfill_module  # noqa: E402
from earthlens.gee.gapfill import GapFill  # noqa: E402


class _Rec:
    """A universal recorder: every method logs and returns itself.

    `map` is explicit so it exercises the per-image function it is handed, which
    is how the interpolation mappers get covered without a live round trip.
    """

    def __init__(self, log: list) -> None:
        self.log = log

    def map(self, fn):
        self.log.append(("map",))
        self.mapped = fn(_Rec(self.log))
        return self

    def __getattr__(self, name: str):
        def _call(*args: Any, **kwargs: Any):
            self.log.append((name, args, kwargs))
            return self

        return _call


class _FakeImageCollection:
    """Callable `ee.ImageCollection` with a `fromImages` constructor."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.built: list = []

    def __call__(self, members):
        self.built.append(members)
        return _Rec(self.log)

    def fromImages(self, members):  # noqa: N802 - mirrors the ee API
        return _Rec(self.log)


class _FakeFilter:
    """Records the join filters a neighbour pairing builds."""

    def __init__(self) -> None:
        self.max_differences: list = []
        self.combinators: list = []

    def And(self, *parts):  # noqa: N802 - mirrors the ee API
        self.combinators.append(parts)
        return ("And", parts)

    def greaterThanOrEquals(self, **kwargs):  # noqa: N802 - mirrors the ee API
        return ("gte", kwargs)

    def lessThanOrEquals(self, **kwargs):  # noqa: N802 - mirrors the ee API
        return ("lte", kwargs)

    def maxDifference(self, **kwargs):  # noqa: N802 - mirrors the ee API
        self.max_differences.append(kwargs["difference"])
        return ("maxDiff", kwargs)


class _FakeJoinInstance:
    """A join whose `apply` records its arguments."""

    def __init__(self, log: list, kwargs: dict) -> None:
        self.log = log
        self.kwargs = kwargs

    def apply(self, **kwargs):
        self.log.append(("apply", self.kwargs["matchesKey"]))
        return ("joined", self.kwargs["matchesKey"])


class _FakeJoin:
    """Records every `saveAll` join built."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.save_all: list = []

    def saveAll(self, **kwargs):  # noqa: N802 - mirrors the ee API
        self.save_all.append(kwargs)
        return _FakeJoinInstance(self.log, kwargs)


class _FakeReducer:
    """Records the `linearRegression` reducer the harmonic fit asks for."""

    def __init__(self) -> None:
        self.linear_regressions: list = []

    def linearRegression(self, **kwargs):  # noqa: N802 - mirrors the ee API
        self.linear_regressions.append(kwargs)
        return ("linearRegression", kwargs)


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the module's `ee` surface with recorders and return them."""
    log: list = []
    fake_filter = _FakeFilter()
    fake_join = _FakeJoin(log)
    fake_reducer = _FakeReducer()
    fake_ic = _FakeImageCollection(log)
    monkeypatch.setattr(gapfill_module.ee, "Image", lambda value=None: _Rec(log))
    monkeypatch.setattr(gapfill_module.ee, "ImageCollection", fake_ic)
    monkeypatch.setattr(gapfill_module.ee, "Filter", fake_filter)
    monkeypatch.setattr(gapfill_module.ee, "Join", fake_join)
    monkeypatch.setattr(gapfill_module.ee, "Reducer", fake_reducer)
    return log, fake_filter, fake_join, fake_reducer


class TestConfig:
    """Construction and validation."""

    def test_the_default_method_is_linear(self):
        """Linear interpolation is the sensible default."""
        assert GapFill(max_gap=dt.timedelta(days=30)).method == "linear"

    def test_a_zero_max_gap_is_refused(self):
        """A zero window would bridge nothing."""
        with pytest.raises(ValidationError):
            GapFill(max_gap=dt.timedelta(0))

    def test_a_negative_max_gap_is_refused(self):
        """A negative window is meaningless."""
        with pytest.raises(ValidationError):
            GapFill(max_gap=dt.timedelta(days=-1))

    def test_harmonic_requires_a_band(self):
        """The harmonic model has nothing to fit without a value band."""
        with pytest.raises(ValidationError):
            GapFill(method="harmonic", max_gap=dt.timedelta(days=60))

    def test_harmonic_with_a_band_is_accepted(self):
        """A named band satisfies the harmonic requirement."""
        assert GapFill(method="harmonic", max_gap=dt.timedelta(days=60), band="NDVI")

    def test_the_config_is_frozen(self):
        """It is a value object; the method cannot be reassigned."""
        with pytest.raises(ValidationError):
            GapFill(max_gap=dt.timedelta(days=30)).method = "nearest"

    def test_the_max_gap_converts_to_milliseconds(self):
        """The join bounds its difference in the millisecond time unit."""
        assert GapFill(max_gap=dt.timedelta(days=1))._max_gap_millis() == 86_400_000


class TestNeighbourJoin:
    """Every method pairs scenes through one within-gap neighbour join."""

    def test_both_before_and_after_joins_are_built(self, fake_ee):
        """A `before` and an `after` neighbour list, each bounded by max_gap."""
        _, _, fake_join, _ = fake_ee
        GapFill(max_gap=dt.timedelta(days=30)).fill(_Rec([]))
        keys = {kwargs["matchesKey"] for kwargs in fake_join.save_all}
        assert keys == {"before", "after"}

    def test_the_join_is_bounded_by_max_gap(self, fake_ee):
        """`maxDifference` is the max_gap window, so nothing joins beyond it."""
        _, fake_filter, _, _ = fake_ee
        GapFill(max_gap=dt.timedelta(days=10)).fill(_Rec([]))
        assert set(fake_filter.max_differences) == {10 * 86_400_000}


class TestLinear:
    """Linear interpolation between the nearest earlier and later pixels."""

    def test_it_interpolates_with_a_time_ratio(self, fake_ee):
        """The time-weighted blend divides, which nearest/harmonic do not."""
        log, _, _, _ = fake_ee
        GapFill(method="linear", max_gap=dt.timedelta(days=30)).fill(_Rec([]))
        names = [entry[0] for entry in log]
        assert "divide" in names
        assert "unmask" in names


class TestNearest:
    """Nearest-neighbour fill, earlier preferred then later."""

    def test_it_unmasks_from_both_neighbours_without_a_ratio(self, fake_ee):
        """Two successive unmasks, and no interpolation ratio."""
        log, _, _, _ = fake_ee
        GapFill(method="nearest", max_gap=dt.timedelta(days=30)).fill(_Rec([]))
        names = [entry[0] for entry in log]
        assert names.count("unmask") >= 2
        assert "divide" not in names


class TestHarmonic:
    """Annual-harmonic fit, predicted only where a within-gap neighbour exists."""

    def test_it_fits_a_linear_regression(self, fake_ee):
        """The harmonic model is a per-pixel linear regression of three terms."""
        _, _, _, fake_reducer = fake_ee
        GapFill(method="harmonic", max_gap=dt.timedelta(days=60), band="NDVI").fill(
            _Rec([])
        )
        (kwargs,) = fake_reducer.linear_regressions
        assert kwargs == {"numX": 3, "numY": 1}

    def test_the_prediction_is_masked_to_within_gap_pixels(self, fake_ee):
        """`updateMask` keeps the fit from extrapolating past max_gap."""
        log, _, _, _ = fake_ee
        GapFill(method="harmonic", max_gap=dt.timedelta(days=60), band="NDVI").fill(
            _Rec([])
        )
        names = [entry[0] for entry in log]
        assert "updateMask" in names
        assert "sin" in names and "cos" in names
