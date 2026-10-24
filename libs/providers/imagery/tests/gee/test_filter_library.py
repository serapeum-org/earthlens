"""Tests for the expanded collection-filter library (Task 6.5)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from earthlens.gee import filters as filters_module
from earthlens.gee.climatology import _SEASONS
from earthlens.gee.configs import EngineConfig
from earthlens.gee.filters import (
    ByDayOfYear,
    ByMonth,
    ByOrbit,
    ByQuality,
    BySeason,
    BySensor,
    CollectionFilter,
)


class _FakeCollection:
    """Records every `filter(...)` argument; each call returns a new stage."""

    def __init__(self, applied=None):
        self.applied = list(applied) if applied is not None else []

    def filter(self, condition):
        return _FakeCollection(applied=[*self.applied, condition])


def _comparison(op):
    """Build a fake binary `ee.Filter` factory tagging its operator."""
    return lambda name, value: (op, name, value)


@pytest.fixture
def fake_ee(monkeypatch):
    """Patch the filters module's `ee` binding with a recording stand-in."""
    fake = SimpleNamespace(
        Filter=SimpleNamespace(
            eq=_comparison("eq"),
            lte=_comparison("lte"),
            gte=_comparison("gte"),
            lt=_comparison("lt"),
            gt=_comparison("gt"),
            inList=lambda name, values: ("inList", name, tuple(values)),
            calendarRange=lambda start, end, unit: ("calendarRange", start, end, unit),
            Or=lambda *conditions: ("or", *conditions),
        )
    )
    monkeypatch.setattr(filters_module, "ee", fake)
    return fake


class TestByOrbit:
    """Tests for `ByOrbit`."""

    def test_filters_on_the_pass_direction(self, fake_ee):
        """A pass direction becomes an `orbitProperties_pass` equality filter."""
        out = ByOrbit(pass_direction="ASCENDING")(_FakeCollection())
        assert out.applied == [("eq", "orbitProperties_pass", "ASCENDING")]

    def test_filters_on_the_relative_orbit(self, fake_ee):
        """A relative orbit becomes a `relativeOrbitNumber_start` equality filter."""
        out = ByOrbit(relative_orbit=87)(_FakeCollection())
        assert out.applied == [("eq", "relativeOrbitNumber_start", 87)]

    def test_both_constraints_compose(self, fake_ee):
        """Setting both applies the pass filter first, then the orbit number."""
        out = ByOrbit(pass_direction="DESCENDING", relative_orbit=87)(_FakeCollection())
        assert out.applied == [
            ("eq", "orbitProperties_pass", "DESCENDING"),
            ("eq", "relativeOrbitNumber_start", 87),
        ]

    def test_neither_constraint_is_refused(self):
        """A filter that would filter nothing is refused at construction."""
        with pytest.raises(ValidationError, match="pass_direction"):
            ByOrbit()

    def test_unknown_pass_direction_is_refused(self):
        """A pass direction outside ASCENDING / DESCENDING is refused."""
        with pytest.raises(ValidationError):
            ByOrbit(pass_direction="SIDEWAYS")

    def test_non_positive_relative_orbit_is_refused(self):
        """Relative orbit numbers start at 1."""
        with pytest.raises(ValidationError):
            ByOrbit(relative_orbit=0)

    def test_is_a_collection_filter(self):
        """The orbit filter is a frozen `CollectionFilter` drop-in."""
        assert isinstance(ByOrbit(relative_orbit=1), CollectionFilter)

    def test_forces_server_side_compute(self):
        """An instance in `filters` flips `needs_server_side_compute`."""
        config = EngineConfig(filters=(ByOrbit(pass_direction="ASCENDING"),))
        assert config.needs_server_side_compute


class TestBySensor:
    """Tests for `BySensor`."""

    def test_filters_on_the_platform_list(self, fake_ee):
        """The sensor names become an `inList` filter on `SPACECRAFT_ID`."""
        out = BySensor(values=("LANDSAT_8", "LANDSAT_9"))(_FakeCollection())
        assert out.applied == [("inList", "SPACECRAFT_ID", ("LANDSAT_8", "LANDSAT_9"))]

    def test_property_name_is_configurable(self, fake_ee):
        """A non-default metadata property is honoured."""
        out = BySensor(values=("Sentinel-2A",), property_name="SPACECRAFT_NAME")(
            _FakeCollection()
        )
        assert out.applied == [("inList", "SPACECRAFT_NAME", ("Sentinel-2A",))]

    def test_empty_values_are_refused(self):
        """At least one sensor name is required."""
        with pytest.raises(ValidationError, match="at least 1"):
            BySensor(values=())


class TestByQuality:
    """Tests for `ByQuality`."""

    @pytest.mark.parametrize("op", ["lte", "gte", "lt", "gt", "eq"])
    def test_dispatches_to_the_matching_ee_filter(self, fake_ee, op):
        """Each operator reaches the matching `ee.Filter` factory."""
        out = ByQuality(property_name="MEAN_SOLAR_ZENITH_ANGLE", op=op, value=70)(
            _FakeCollection()
        )
        assert out.applied == [(op, "MEAN_SOLAR_ZENITH_ANGLE", 70.0)]

    def test_unknown_op_is_refused(self):
        """An operator outside the allowed set is refused at construction."""
        with pytest.raises(ValidationError):
            ByQuality(property_name="CLOUD_COVER", op="neq", value=1)


class TestBySeason:
    """Tests for `BySeason`."""

    def test_djf_expands_to_an_or_over_its_months(self, fake_ee):
        """The wrap-around season is an `Or` over months 12, 1 and 2."""
        out = BySeason(season="DJF")(_FakeCollection())
        assert out.applied == [
            (
                "or",
                ("calendarRange", 12, 12, "month"),
                ("calendarRange", 1, 1, "month"),
                ("calendarRange", 2, 2, "month"),
            )
        ]

    @pytest.mark.parametrize("season", list(_SEASONS))
    def test_every_season_matches_the_climatology_expansion(self, fake_ee, season):
        """Each season filters exactly the months `Climatology` groups by."""
        out = BySeason(season=season)(_FakeCollection())
        months = tuple(
            ("calendarRange", month, month, "month") for month in _SEASONS[season]
        )
        assert out.applied == [("or", *months)]

    def test_unknown_season_is_refused(self):
        """A season name outside DJF / MAM / JJA / SON is refused."""
        with pytest.raises(ValidationError):
            BySeason(season="WINTER")


class TestByMonth:
    """Tests for `ByMonth`."""

    def test_single_month(self, fake_ee):
        """Omitting the end month selects exactly the start month."""
        out = ByMonth(start_month=6)(_FakeCollection())
        assert out.applied == [("calendarRange", 6, 6, "month")]

    def test_month_window(self, fake_ee):
        """A start and end month select the inclusive window across years."""
        out = ByMonth(start_month=6, end_month=8)(_FakeCollection())
        assert out.applied == [("calendarRange", 6, 8, "month")]

    def test_wrapping_window(self, fake_ee):
        """A start after the end wraps across the year boundary (EE semantics)."""
        out = ByMonth(start_month=11, end_month=2)(_FakeCollection())
        assert out.applied == [("calendarRange", 11, 2, "month")]

    @pytest.mark.parametrize("field", ["start_month", "end_month"])
    @pytest.mark.parametrize("value", [0, 13])
    def test_out_of_range_month_is_refused(self, field, value):
        """A month outside 1-12 is refused at construction."""
        with pytest.raises(ValidationError):
            ByMonth(**{"start_month": 6, field: value})


class TestByDayOfYear:
    """Tests for `ByDayOfYear`."""

    def test_single_day(self, fake_ee):
        """Omitting the end day selects exactly the start day."""
        out = ByDayOfYear(start_day=100)(_FakeCollection())
        assert out.applied == [("calendarRange", 100, 100, "day_of_year")]

    def test_day_window(self, fake_ee):
        """A start and end day select the inclusive window across years."""
        out = ByDayOfYear(start_day=152, end_day=243)(_FakeCollection())
        assert out.applied == [("calendarRange", 152, 243, "day_of_year")]

    @pytest.mark.parametrize("field", ["start_day", "end_day"])
    @pytest.mark.parametrize("value", [0, 367])
    def test_out_of_range_day_is_refused(self, field, value):
        """A day outside 1-366 is refused at construction."""
        with pytest.raises(ValidationError):
            ByDayOfYear(**{"start_day": 100, field: value})
