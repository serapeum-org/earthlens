"""Tests for the per-period `Climatology` and its anomaly companion."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import climatology as clim_module  # noqa: E402
from earthlens.gee.climatology import Climatology  # noqa: E402
from earthlens.gee.reducers import TemporalReducer  # noqa: E402


class _Rec:
    """A recording stand-in whose every method returns itself."""

    def __init__(self, log: list, tag: str = "image") -> None:
        self.log = log
        self.tag = tag

    def __getattr__(self, name: str):
        def _call(*args: Any, **kwargs: Any):
            self.log.append((self.tag, name, args, kwargs))
            return self

        return _call


class _FakeCollection:
    """Records every filter applied, returning itself so filters chain."""

    def __init__(self, log: list) -> None:
        self.log = log

    def filter(self, condition):
        self.log.append(("collection", "filter", condition))
        return self


class _FakeFilter:
    """Records `calendarRange` / `Or` so period filters can be asserted."""

    def __init__(self) -> None:
        self.calendar_ranges: list = []
        self.or_calls: list = []

    def calendarRange(self, start, end, field):  # noqa: N802 - mirrors the ee API
        self.calendar_ranges.append((start, end, field))
        return ("calendarRange", start, end, field)

    def Or(self, *filters):  # noqa: N802 - mirrors the ee API
        self.or_calls.append(filters)
        return ("Or", filters)


class _FakeReducer(TemporalReducer):
    """A temporal reducer whose `apply_ee` records the collection it reduced."""

    name = "mean"

    def __init__(self) -> None:
        self.applied: list = []

    def __call__(self, collection):
        return self.apply_ee(collection)

    def apply_ee(self, collection):
        self.applied.append(collection)
        return _Rec([], tag="reduced")


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the module's `ee.Filter` / `ee.Image` / `ee.ImageCollection`."""
    fake_filter = _FakeFilter()
    images: list = []
    collections: list = []
    monkeypatch.setattr(clim_module.ee, "Filter", fake_filter, raising=False)
    monkeypatch.setattr(
        clim_module.ee,
        "Image",
        lambda value: _Rec([], tag="wrapped"),
        raising=False,
    )
    monkeypatch.setattr(
        clim_module.ee,
        "ImageCollection",
        lambda members: collections.append(members) or ("collection", members),
        raising=False,
    )
    return fake_filter, collections


class TestConfig:
    """Construction, reducer coercion and baseline validation."""

    def test_the_default_reducer_is_the_mean(self):
        """No fourth reducer vocabulary: it resolves through the Epic-1 registry."""
        assert Climatology(period="month", baseline=(1991, 2020)).reducer.name == "mean"

    def test_a_reducer_string_is_resolved(self):
        """A percentile shorthand resolves to the registry's reducer."""
        clim = Climatology(period="month", baseline=(1991, 2020), reducer="p90")
        assert clim.reducer.q == 90.0

    def test_a_region_only_reducer_is_refused(self):
        """`stdDev` has no time-axis form, so it fails closed at construction."""
        with pytest.raises(ValidationError):
            Climatology(period="month", baseline=(1991, 2020), reducer="stdDev")

    def test_a_reversed_baseline_is_refused(self):
        """The reference window must be ordered."""
        with pytest.raises(ValidationError):
            Climatology(period="month", baseline=(2020, 1991))

    def test_the_config_is_frozen(self):
        """It is a value object; the period cannot be reassigned."""
        clim = Climatology(period="month", baseline=(1991, 2020))
        with pytest.raises(ValidationError):
            clim.period = "season"


class TestPeriods:
    """The grouping values differ from consecutive-bucket cadences."""

    def test_month_groups_are_one_to_twelve(self):
        """Twelve calendar months, across years."""
        assert Climatology(period="month", baseline=(1, 2)).periods() == list(
            range(1, 13)
        )

    def test_dayofyear_groups_cover_a_leap_year(self):
        """366 day-of-year groups so the leap day is not dropped."""
        assert len(Climatology(period="dayofyear", baseline=(1, 2)).periods()) == 366

    def test_season_groups_are_the_four_names(self):
        """Seasons are named groups of months, not a single calendar field."""
        assert Climatology(period="season", baseline=(1, 2)).periods() == [
            "DJF",
            "MAM",
            "JJA",
            "SON",
        ]


class TestClimatology:
    """`climatology()` reduces the baseline years, one image per period."""

    def test_it_filters_to_the_baseline_years(self, fake_ee):
        """Only scenes in [start, end] contribute to the climatology."""
        fake_filter, _ = fake_ee
        reducer = _FakeReducer()
        clim = Climatology(period="season", baseline=(1991, 2020), reducer=reducer)
        clim.climatology(_FakeCollection([]))
        assert (1991, 2020, "year") in fake_filter.calendar_ranges

    def test_it_reduces_once_per_period(self, fake_ee):
        """A season climatology reduces the four groups."""
        _, collections = fake_ee
        reducer = _FakeReducer()
        clim = Climatology(period="season", baseline=(1991, 2020), reducer=reducer)
        clim.climatology(_FakeCollection([]))
        assert len(reducer.applied) == 4
        assert len(collections) == 1

    def test_a_season_expands_to_an_or_over_its_months(self, fake_ee):
        """`DJF` matches December, January and February."""
        fake_filter, _ = fake_ee
        reducer = _FakeReducer()
        clim = Climatology(period="season", baseline=(1991, 2020), reducer=reducer)
        clim.climatology(_FakeCollection([]))
        months_or = {
            tuple((start, end) for _tag, start, end, _field in filters)
            for filters in fake_filter.or_calls
        }
        assert ((12, 12), (1, 1), (2, 2)) in months_or

    def test_month_uses_the_calendar_month_unit(self, fake_ee):
        """A month climatology filters on the `month` calendar field."""
        fake_filter, _ = fake_ee
        reducer = _FakeReducer()
        clim = Climatology(period="month", baseline=(2000, 2010), reducer=reducer)
        clim.climatology(_FakeCollection([]))
        month_ranges = [r for r in fake_filter.calendar_ranges if r[2] == "month"]
        assert (6, 6, "month") in month_ranges

    def test_dayofyear_uses_the_day_of_year_unit(self, fake_ee):
        """Day-of-year groups filter on the `day_of_year` calendar field."""
        fake_filter, _ = fake_ee
        reducer = _FakeReducer()
        clim = Climatology(period="dayofyear", baseline=(2000, 2010), reducer=reducer)
        clim.climatology(_FakeCollection([]))
        doy_ranges = [r for r in fake_filter.calendar_ranges if r[2] == "day_of_year"]
        assert (152, 152, "day_of_year") in doy_ranges


class TestAnomaly:
    """`anomaly()` is the observed period minus its baseline climatology."""

    def test_it_subtracts_the_baseline_from_the_observed(self, fake_ee):
        """The companion to a climatology, computed in one call."""
        reducer = _FakeReducer()
        clim = Climatology(period="month", baseline=(1991, 2020), reducer=reducer)
        result = clim.anomaly(_FakeCollection([]), _FakeCollection([]), 6)
        # observed and baseline are each reduced once.
        assert len(reducer.applied) == 2
        assert result.tag == "wrapped"

    def test_an_out_of_range_month_is_refused(self, fake_ee):
        """A month outside 1-12 fails loudly, not silently."""
        clim = Climatology(period="month", baseline=(1991, 2020))
        observed = _FakeCollection([])
        baseline = _FakeCollection([])
        with pytest.raises(ValueError, match="valid month"):
            clim.anomaly(observed, baseline, 13)

    def test_an_unknown_season_is_refused(self, fake_ee):
        """A season name outside the four is refused."""
        clim = Climatology(period="season", baseline=(1991, 2020))
        observed = _FakeCollection([])
        baseline = _FakeCollection([])
        with pytest.raises(ValueError, match="not a season"):
            clim.anomaly(observed, baseline, "SUMMER")

    def test_a_valid_season_anomaly_runs(self, fake_ee):
        """A season anomaly validates and reduces both sides."""
        reducer = _FakeReducer()
        clim = Climatology(period="season", baseline=(1991, 2020), reducer=reducer)
        clim.anomaly(_FakeCollection([]), _FakeCollection([]), "JJA")
        assert len(reducer.applied) == 2
