"""Tests for the `earthlens.gee.filters` callable classes (Task 6.1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from earthlens.gee import filters as filters_module
from earthlens.gee.configs import EngineConfig
from earthlens.gee.filters import (
    ByBounds,
    ByCloudCoverLte,
    ByPropertyIn,
    ByYear,
    ByYearAndBounds,
    CollectionFilter,
    by_bounds,
    by_cloud_cover_lte,
    by_property_in,
    by_year,
    by_year_and_bounds,
)


class _FakeCollection:
    """Chainable recorder standing in for an `ee.ImageCollection`."""

    def __init__(self, calls=None):
        self.calls: list[tuple[str, tuple]] = list(calls or [])

    def _chain(self, name, *args):
        return _FakeCollection(self.calls + [(name, args)])

    def filterDate(self, start, end):  # noqa: N802
        return self._chain("filterDate", start, end)

    def filterBounds(self, region):  # noqa: N802
        return self._chain("filterBounds", region)

    def filter(self, filt):
        return self._chain("filter", filt)


@pytest.fixture
def fake_ee(monkeypatch):
    """Replace the module's `ee.Filter` with recording stand-ins."""
    fake = SimpleNamespace(
        Filter=SimpleNamespace(
            inList=lambda name, values: ("inList", name, tuple(values)),
            lte=lambda name, value: ("lte", name, value),
        )
    )
    monkeypatch.setattr(filters_module, "ee", fake)
    return fake


class TestCollectionFilterBase:
    """Tests for the class-form `CollectionFilter` base."""

    def test_base_call_is_abstract(self):
        """Calling the base class directly raises `NotImplementedError`."""
        base = CollectionFilter()
        collection = _FakeCollection()
        with pytest.raises(NotImplementedError, match="CollectionFilter"):
            base(collection)

    def test_is_frozen(self):
        """A constructed filter is immutable."""
        year_filter = ByYear(year=2020)
        with pytest.raises(ValidationError):
            year_filter.year = 2021

    def test_rejects_unknown_fields(self):
        """`extra="forbid"` refuses a typo'd keyword at construction."""
        with pytest.raises(ValidationError):
            ByYear(yeer=2020)

    def test_instance_forces_server_side_compute(self):
        """An instance in `filters` flips `needs_server_side_compute`."""
        assert EngineConfig(filters=(ByYear(year=2020),)).needs_server_side_compute

    def test_equality_is_by_value(self):
        """Two filters with the same config compare equal."""
        # Bound to locals so the comparison reads as two distinct instances,
        # which is the point: a frozen pydantic model compares by value.
        first = ByYear(year=2020)
        same = ByYear(year=2020)
        other = ByYear(year=2021)
        assert first is not same
        assert first == same
        assert first != other


class TestByYear:
    """Tests for `ByYear` and the `by_year` wrapper."""

    def test_filters_to_the_calendar_year(self):
        """The collection is date-filtered to `[year, year+1)`."""
        out = ByYear(year=2020)(_FakeCollection())
        assert out.calls == [("filterDate", ("2020-01-01", "2021-01-01"))]

    def test_wrapper_delegates_to_the_class(self):
        """`by_year` records the same call graph as a class instance."""
        assert by_year(_FakeCollection(), 1999).calls == (
            ByYear(year=1999)(_FakeCollection()).calls
        )


class TestByBounds:
    """Tests for `ByBounds` and the `by_bounds` wrapper."""

    def test_filters_to_the_region(self):
        """The collection is bounds-filtered to the given region."""
        region = object()
        out = ByBounds(region=region)(_FakeCollection())
        assert out.calls == [("filterBounds", (region,))]

    def test_wrapper_delegates_to_the_class(self):
        """`by_bounds` records the same call graph as a class instance."""
        region = object()
        assert by_bounds(_FakeCollection(), region).calls == (
            ByBounds(region=region)(_FakeCollection()).calls
        )


class TestByPropertyIn:
    """Tests for `ByPropertyIn` and the `by_property_in` wrapper."""

    def test_filters_by_membership(self, fake_ee):
        """The collection is filtered by `ee.Filter.inList` over the values."""
        out = ByPropertyIn(property_name="WRS_PATH", values=(176, 177))(
            _FakeCollection()
        )
        assert out.calls == [("filter", (("inList", "WRS_PATH", (176, 177)),))]

    def test_values_normalised_to_tuple(self):
        """A list of values is stored as a tuple on the frozen model."""
        assert ByPropertyIn(property_name="p", values=[1, 2]).values == (1, 2)

    def test_wrapper_accepts_any_iterable(self, fake_ee):
        """`by_property_in` materialises a one-shot generator like the original."""
        out = by_property_in(_FakeCollection(), "p", (v for v in (1, 2)))
        assert out.calls == [("filter", (("inList", "p", (1, 2)),))]


class TestByCloudCoverLte:
    """Tests for `ByCloudCoverLte` and the `by_cloud_cover_lte` wrapper."""

    def test_filters_by_cloud_cover(self, fake_ee):
        """The collection is filtered by `ee.Filter.lte` on the default property."""
        out = ByCloudCoverLte(max_pct=20)(_FakeCollection())
        assert out.calls == [("filter", (("lte", "CLOUD_COVER", 20.0),))]

    def test_custom_property_name(self, fake_ee):
        """A Sentinel-2 property name is threaded through to the filter."""
        out = ByCloudCoverLte(max_pct=35, property_name="CLOUDY_PIXEL_PERCENTAGE")(
            _FakeCollection()
        )
        assert out.calls == [("filter", (("lte", "CLOUDY_PIXEL_PERCENTAGE", 35.0),))]

    @pytest.mark.parametrize("max_pct", [0, 100])
    def test_boundary_percentages_accepted(self, max_pct):
        """The inclusive bounds of `[0, 100]` construct fine."""
        assert ByCloudCoverLte(max_pct=max_pct).max_pct == max_pct

    @pytest.mark.parametrize("max_pct", [-0.1, 100.1])
    def test_out_of_range_rejected_at_construction(self, max_pct):
        """A percentage outside `[0, 100]` raises with the known message."""
        with pytest.raises(ValidationError, match=r"max_pct must be in \[0, 100\]"):
            ByCloudCoverLte(max_pct=max_pct)

    def test_wrapper_preserves_the_value_error(self, fake_ee):
        """`by_cloud_cover_lte` still raises `ValueError` for a bad percentage."""
        collection = _FakeCollection()
        with pytest.raises(ValueError, match=r"max_pct must be in \[0, 100\]"):
            by_cloud_cover_lte(collection, 150)

    def test_wrapper_delegates_to_the_class(self, fake_ee):
        """`by_cloud_cover_lte` records the same call graph as a class instance."""
        assert by_cloud_cover_lte(_FakeCollection(), 20, property_name="CC").calls == (
            ByCloudCoverLte(max_pct=20, property_name="CC")(_FakeCollection()).calls
        )


class TestByYearAndBounds:
    """Tests for `ByYearAndBounds` and the `by_year_and_bounds` wrapper."""

    def test_year_only_when_region_omitted(self):
        """Without a region only the year filter is applied."""
        out = ByYearAndBounds(year=2020)(_FakeCollection())
        assert out.calls == [("filterDate", ("2020-01-01", "2021-01-01"))]

    def test_year_then_bounds_when_region_given(self):
        """With a region the year filter is composed with the bounds filter."""
        region = object()
        out = ByYearAndBounds(year=2020, region=region)(_FakeCollection())
        assert out.calls == [
            ("filterDate", ("2020-01-01", "2021-01-01")),
            ("filterBounds", (region,)),
        ]

    def test_wrapper_delegates_to_the_class(self):
        """`by_year_and_bounds` records the same call graph as a class instance."""
        region = object()
        assert by_year_and_bounds(_FakeCollection(), 2020, region).calls == (
            ByYearAndBounds(year=2020, region=region)(_FakeCollection()).calls
        )
