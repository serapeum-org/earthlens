"""Property-based tests for `earthlens.base.resolve_aoi`.

The invariant is normalisation: every accepted input shape reduces to the same
ordered `([S, N], [W, E])` extent, and an inverted or antimeridian-crossing box
is rejected rather than silently transposed.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from earthlens.base import resolve_aoi

_LATS = st.floats(
    min_value=-90.0, max_value=90.0, allow_nan=False, allow_infinity=False
)
_LONS = st.floats(
    min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False
)
_LON_PAIR = st.lists(_LONS, min_size=2, max_size=2)
_LAT_PAIR = st.lists(_LATS, min_size=2, max_size=2)


@pytest.mark.unit
class TestResolveAoiProperties:
    """resolve_aoi normalises every accepted shape to one ordered extent."""

    @given(lons=_LON_PAIR, lats=_LAT_PAIR)
    def test_valid_bbox_is_never_transposed(self, lons, lats):
        """A W<=E, S<=N bbox returns ordered [S, N] / [W, E] and no polygon mask."""
        west, east = sorted(lons)
        south, north = sorted(lats)
        lat_lim, lon_lim, geom = resolve_aoi([west, south, east, north])
        assert lat_lim == [south, north], lat_lim
        assert lon_lim == [west, east], lon_lim
        assert lat_lim[0] <= lat_lim[1] and lon_lim[0] <= lon_lim[1]
        assert geom is None, "a plain bbox has no polygon mask"

    @given(lons=_LON_PAIR, lats=_LAT_PAIR)
    def test_inverted_latitude_raises(self, lons, lats):
        """South edge north of the north edge is rejected, not transposed."""
        assume(lats[0] != lats[1])
        west, east = sorted(lons)
        low, high = sorted(lats)
        south, north = high, low  # south > north
        with pytest.raises(ValueError, match="inverted latitude"):
            resolve_aoi([west, south, east, north])

    @given(lons=_LON_PAIR, lats=_LAT_PAIR)
    def test_west_east_of_east_raises_antimeridian(self, lons, lats):
        """West east of east reads as an antimeridian crossing and is rejected."""
        assume(lons[0] != lons[1])
        low, high = sorted(lons)
        west, east = high, low  # west > east
        south, north = sorted(lats)
        with pytest.raises(ValueError, match="antimeridian"):
            resolve_aoi([west, south, east, north])

    @given(lons=_LON_PAIR, lats=_LAT_PAIR)
    def test_list_and_compass_dict_forms_agree(self, lons, lats):
        """The bbox list and the compass-dict spelling normalise identically."""
        west, east = sorted(lons)
        south, north = sorted(lats)
        from_list = resolve_aoi([west, south, east, north])[:2]
        from_dict = resolve_aoi(
            {"west": west, "south": south, "east": east, "north": north}
        )[:2]
        assert from_list == from_dict, (from_list, from_dict)

    @given(
        lon=_LONS,
        lat=_LATS,
        buffer=st.floats(
            min_value=1e-3, max_value=45.0, allow_nan=False, allow_infinity=False
        ),
    )
    def test_point_with_buffer_contains_it_and_stays_in_range(self, lon, lat, buffer):
        """A buffered point yields an ordered box that contains it and stays valid."""
        lat_lim, lon_lim, geom = resolve_aoi([lon, lat], buffer=buffer)
        assert lat_lim[0] <= lat <= lat_lim[1], lat_lim
        assert lon_lim[0] <= lon <= lon_lim[1], lon_lim
        assert -90.0 <= lat_lim[0] <= lat_lim[1] <= 90.0, lat_lim
        assert -180.0 <= lon_lim[0] <= lon_lim[1] <= 180.0, lon_lim
        assert geom is None

    @given(lon=_LONS, lat=_LATS)
    def test_point_without_buffer_raises(self, lon, lat):
        """A two-value point aoi with no buffer has no area and is rejected."""
        with pytest.raises(ValueError, match="buffer"):
            resolve_aoi([lon, lat])
