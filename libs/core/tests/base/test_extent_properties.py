"""Property-based tests for the frozen extent models.

`SpatialExtent` and `TemporalExtent` accept any ordered bounds and reject any
inverted, out-of-range, or (for the spatial box) non-positive-resolution ones,
rather than silently normalising them. Both are frozen once built.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from earthlens.base import SpatialExtent, TemporalExtent

_LATS = st.floats(
    min_value=-90.0, max_value=90.0, allow_nan=False, allow_infinity=False
)
_LONS = st.floats(
    min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False
)
_RESOLUTION = st.one_of(
    st.none(),
    st.floats(min_value=1e-6, max_value=10.0, allow_nan=False, allow_infinity=False),
)
_OUT_OF_LAT = st.one_of(
    st.floats(min_value=90.0001, max_value=1e4, allow_nan=False, allow_infinity=False),
    st.floats(
        min_value=-1e4, max_value=-90.0001, allow_nan=False, allow_infinity=False
    ),
)
_OUT_OF_LON = st.one_of(
    st.floats(min_value=180.0001, max_value=1e5, allow_nan=False, allow_infinity=False),
    st.floats(
        min_value=-1e5, max_value=-180.0001, allow_nan=False, allow_infinity=False
    ),
)
_DATES = st.dates(min_value=dt.date(1900, 1, 1), max_value=dt.date(2100, 1, 1))


@pytest.mark.unit
class TestSpatialExtentProperties:
    """SpatialExtent accepts ordered, in-range boxes and rejects the rest."""

    @given(
        lons=st.lists(_LONS, min_size=2, max_size=2),
        lats=st.lists(_LATS, min_size=2, max_size=2),
        resolution=_RESOLUTION,
    )
    def test_ordered_box_constructs_and_preserves_fields(self, lons, lats, resolution):
        """An in-range box with min<=max on both axes builds and round-trips."""
        west, east = sorted(lons)
        south, north = sorted(lats)
        extent = SpatialExtent(
            latitude_min=south,
            latitude_max=north,
            longitude_min=west,
            longitude_max=east,
            resolution=resolution,
        )
        assert extent.latitude_min <= extent.latitude_max
        assert extent.longitude_min <= extent.longitude_max
        assert (extent.latitude_min, extent.latitude_max) == (south, north)
        assert (extent.longitude_min, extent.longitude_max) == (west, east)

    @given(lats=st.lists(_LATS, min_size=2, max_size=2), lon=_LONS)
    def test_inverted_latitude_is_rejected(self, lats, lon):
        """latitude_min above latitude_max is an error, not a transposition."""
        assume(lats[0] != lats[1])
        low, high = sorted(lats)
        with pytest.raises(ValidationError):
            SpatialExtent(
                latitude_min=high,
                latitude_max=low,
                longitude_min=lon,
                longitude_max=lon,
            )

    @given(lons=st.lists(_LONS, min_size=2, max_size=2), lat=_LATS)
    def test_inverted_longitude_is_rejected(self, lons, lat):
        """longitude_min above longitude_max is an error, not a transposition."""
        assume(lons[0] != lons[1])
        low, high = sorted(lons)
        with pytest.raises(ValidationError):
            SpatialExtent(
                latitude_min=lat,
                latitude_max=lat,
                longitude_min=high,
                longitude_max=low,
            )

    @given(bad_lat=_OUT_OF_LAT)
    def test_out_of_range_latitude_is_rejected(self, bad_lat):
        """A latitude outside [-90, 90] fails the per-field bound."""
        lat_min, lat_max = min(bad_lat, 0.0), max(bad_lat, 0.0)
        with pytest.raises(ValidationError):
            SpatialExtent(
                latitude_min=lat_min,
                latitude_max=lat_max,
                longitude_min=0.0,
                longitude_max=0.0,
            )

    @given(bad_lon=_OUT_OF_LON)
    def test_out_of_range_longitude_is_rejected(self, bad_lon):
        """A longitude outside [-180, 180] fails the per-field bound."""
        lon_min, lon_max = min(bad_lon, 0.0), max(bad_lon, 0.0)
        with pytest.raises(ValidationError):
            SpatialExtent(
                latitude_min=0.0,
                latitude_max=0.0,
                longitude_min=lon_min,
                longitude_max=lon_max,
            )

    @given(
        resolution=st.floats(
            max_value=0.0, min_value=-1e4, allow_nan=False, allow_infinity=False
        )
    )
    def test_non_positive_resolution_is_rejected(self, resolution):
        """resolution must be strictly positive when given."""
        with pytest.raises(ValidationError):
            SpatialExtent(
                latitude_min=0.0,
                latitude_max=0.0,
                longitude_min=0.0,
                longitude_max=0.0,
                resolution=resolution,
            )

    def test_is_frozen(self):
        """A built extent cannot be mutated."""
        extent = SpatialExtent(
            latitude_min=0.0, latitude_max=1.0, longitude_min=0.0, longitude_max=1.0
        )
        with pytest.raises(ValidationError):
            extent.latitude_min = -5.0


@pytest.mark.unit
class TestTemporalExtentProperties:
    """TemporalExtent enforces start_date <= end_date unless a half is None."""

    @given(dates=st.lists(_DATES, min_size=2, max_size=2))
    def test_ordered_bounds_construct_and_preserve(self, dates):
        """start_date <= end_date builds and keeps both bounds."""
        start, end = sorted(dates)
        extent = TemporalExtent(
            start_date=start, end_date=end, resolution="D", dates=()
        )
        assert extent.start_date == start, extent.start_date
        assert extent.end_date == end, extent.end_date

    @given(dates=st.lists(_DATES, min_size=2, max_size=2))
    def test_inverted_bounds_are_rejected(self, dates):
        """start_date after end_date is an inverted window and is rejected."""
        assume(dates[0] != dates[1])
        low, high = sorted(dates)
        with pytest.raises(ValidationError):
            TemporalExtent(start_date=high, end_date=low, resolution="D", dates=())

    @given(present=_DATES)
    def test_an_open_half_skips_the_ordering_check(self, present):
        """A None start or end is open-ended, so the ordering check is skipped."""
        assert (
            TemporalExtent(
                start_date=None, end_date=present, resolution="D", dates=()
            ).start_date
            is None
        )
        assert (
            TemporalExtent(
                start_date=present, end_date=None, resolution="D", dates=()
            ).end_date
            is None
        )

    def test_is_frozen(self):
        """A built extent cannot be mutated."""
        extent = TemporalExtent(
            start_date=dt.date(2020, 1, 1),
            end_date=dt.date(2020, 2, 1),
            resolution="D",
            dates=(),
        )
        with pytest.raises(ValidationError):
            extent.resolution = "MS"
