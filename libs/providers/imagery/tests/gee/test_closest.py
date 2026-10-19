"""Tests for `ClosestImage` — the scene nearest a target date."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import closest as closest_module  # noqa: E402
from earthlens.gee.closest import ClosestImage  # noqa: E402


class _FakeNumber:
    """Records the `subtract(...).abs()` chain a delta stamp builds."""

    def __init__(self, log: list, value: Any) -> None:
        self.log = log
        self.value = value

    def subtract(self, other):
        self.log.append(("subtract", other))
        return self

    def abs(self):
        self.log.append(("abs",))
        return self


class _FakeImage:
    """A scene whose `set` records the property it was stamped with."""

    def __init__(self, log: list) -> None:
        self.log = log

    def get(self, key):
        return 10_000

    def set(self, key, value):
        self.log.append(("set", key))
        return self


class _FakeCollection:
    """Applies `map` to a fake scene and records `filter` / `sort` / `first`."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.filtered_on: list = []
        self.sorted_on: list = []

    def map(self, fn):
        fn(_FakeImage(self.log))
        return self

    def filter(self, condition):
        self.filtered_on.append(condition)
        return self

    def sort(self, field):
        self.sorted_on.append(field)
        return self

    def first(self):
        return "nearest-image"


class _FakeFilter:
    """Records the `lte(field, value)` tolerance filter."""

    def __init__(self) -> None:
        self.lte_calls: list = []

    def lte(self, field, value):
        self.lte_calls.append((field, value))
        return ("lte", field, value)


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the module's `ee.Number` / `ee.Filter` / `ee.Image`."""
    number_log: list = []
    fake_filter = _FakeFilter()
    monkeypatch.setattr(
        closest_module.ee,
        "Number",
        lambda value: _FakeNumber(number_log, value),
        raising=False,
    )
    monkeypatch.setattr(closest_module.ee, "Filter", fake_filter, raising=False)
    monkeypatch.setattr(
        closest_module.ee, "Image", lambda value: ("wrapped", value), raising=False
    )
    return fake_filter


class TestConfig:
    """Construction and tolerance validation."""

    def test_the_tolerance_defaults_to_none(self):
        """No bound means the nearest scene is always returned."""
        assert ClosestImage(target=dt.date(2024, 6, 1)).tolerance is None

    def test_a_zero_tolerance_is_refused(self):
        """A zero-width window would exclude every scene."""
        with pytest.raises(ValidationError):
            ClosestImage(target=dt.date(2024, 6, 1), tolerance=dt.timedelta(0))

    def test_a_negative_tolerance_is_refused(self):
        """A negative window is meaningless."""
        with pytest.raises(ValidationError):
            ClosestImage(target=dt.date(2024, 6, 1), tolerance=dt.timedelta(days=-1))

    def test_the_target_converts_to_epoch_millis(self):
        """The target stamps on the same scale as `system:time_start`."""
        assert ClosestImage(target=dt.date(1970, 1, 2))._target_millis() == 86_400_000

    def test_the_config_is_frozen(self):
        """It is a value object; the target cannot be reassigned."""
        with pytest.raises(ValidationError):
            ClosestImage(target=dt.date(2024, 6, 1)).target = dt.date(2024, 6, 2)


class TestSelect:
    """`select()` stamps distance, honours a tolerance, and sorts to nearest."""

    def test_it_stamps_each_scene_with_its_distance(self, fake_ee):
        """Every scene carries an absolute-distance property to sort on."""
        log: list = []
        ClosestImage(target=dt.date(2024, 6, 1)).select(_FakeCollection(log))
        assert ("set", "_closest_delta_millis") in log

    def test_without_a_tolerance_no_filter_is_applied(self, fake_ee):
        """The unbounded search only sorts; nothing is dropped."""
        collection = _FakeCollection([])
        ClosestImage(target=dt.date(2024, 6, 1)).select(collection)
        assert collection.filtered_on == []
        assert collection.sorted_on == ["_closest_delta_millis"]

    def test_a_tolerance_filters_within_the_window(self, fake_ee):
        """A tolerance drops every scene beyond it before the nearest is taken."""
        collection = _FakeCollection([])
        ClosestImage(target=dt.date(2024, 6, 1), tolerance=dt.timedelta(days=5)).select(
            collection
        )
        assert fake_ee.lte_calls == [("_closest_delta_millis", 5 * 86_400_000)]
        assert collection.filtered_on == [("lte", "_closest_delta_millis", 432_000_000)]

    def test_it_returns_the_nearest_image_wrapped(self, fake_ee):
        """The sorted collection's first image is the answer."""
        result = ClosestImage(target=dt.date(2024, 6, 1)).select(_FakeCollection([]))
        assert result == ("wrapped", "nearest-image")
