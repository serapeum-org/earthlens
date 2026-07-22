"""Tests for `earthlens.base.spatial` — backend-agnostic geometry helpers."""

from __future__ import annotations

import math

import pytest
from earthlens.base.spatial import METRES_PER_DEGREE, estimate_pixel_dims

from earthlens.base import SpatialExtent


class TestEstimatePixelDims:
    """Tests for the free-function form `estimate_pixel_dims(w, s, e, n, scale)`."""

    def test_small_box_at_90m(self):
        """A 0.1°×0.1° box at 90 m is ~124×125 px (matches the docstring example)."""
        assert estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 90.0) == (124, 125)

    def test_finer_scale_more_pixels(self):
        """A finer `scale_m` yields a larger pixel grid."""
        w90, _ = estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 90.0)
        w10, _ = estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 10.0)
        assert w10 > w90 * 8

    def test_minimum_one_pixel(self):
        """A sub-pixel bbox still reports at least 1×1."""
        assert estimate_pixel_dims(0.0, 0.0, 0.0001, 0.0001, 5000.0) == (1, 1)

    @pytest.mark.parametrize("bad_scale", [0.0, -1.0, -90.0])
    def test_non_positive_scale_raises(self, bad_scale):
        """A non-positive `scale_m` raises `ValueError`."""
        with pytest.raises(ValueError, match="scale_m must be positive"):
            estimate_pixel_dims(0.0, 0.0, 1.0, 1.0, bad_scale)

    def test_inverted_longitude_raises(self):
        """`east < west` raises rather than producing a negative pixel count."""
        with pytest.raises(ValueError, match="east"):
            estimate_pixel_dims(2.0, 0.0, 1.0, 1.0, 30.0)

    def test_inverted_latitude_raises(self):
        """`north < south` raises rather than producing a negative pixel count."""
        with pytest.raises(ValueError, match="north"):
            estimate_pixel_dims(0.0, 2.0, 1.0, 1.0, 30.0)


class TestMetresPerDegree:
    """The exported constant, which no longer backs `estimate_pixel_dims`."""

    def test_matches_the_equatorial_approximation(self):
        """The exported constant matches the equatorial WGS84 approximation."""
        assert METRES_PER_DEGREE == pytest.approx(111_320.0)

    def test_does_not_predict_estimate_pixel_dims_height(self):
        """The constant no longer describes the height the sizing returns.

        `estimate_pixel_dims` delegates to pyramids, which sizes the latitude
        axis with the polar-maximum 111_694 m/deg. Pinning this keeps the
        constant from being read as the function's basis: a caller predicting
        the height from it would be wrong, and would then over-trust a
        pre-flight size guard.
        """
        _, height = estimate_pixel_dims(31.0, 30.0, 31.1, 30.1, 90.0)
        predicted = math.ceil(0.1 / (90.0 / METRES_PER_DEGREE))
        assert height != predicted
        assert height > predicted  # pyramids over-counts: the safe direction


class TestSpatialExtentMethod:
    """Tests for the `SpatialExtent.estimate_pixel_dims(scale_m)` wrapper."""

    def test_delegates_to_free_function(self):
        """The method form returns the same result as the free function."""
        box = SpatialExtent.from_pairs([30.0, 30.1], [31.0, 31.1])
        assert box.estimate_pixel_dims(90.0) == estimate_pixel_dims(
            31.0, 30.0, 31.1, 30.1, 90.0
        )

    def test_method_round_trip(self):
        """Sanity: SRTM at 30 m over a 1° box is roughly 3700 px.

        The height runs a little above the width: the latitude axis is sized
        with the polar-maximum metres-per-degree, the longitude axis with the
        equatorial one.
        """
        box = SpatialExtent.from_pairs([0.0, 1.0], [0.0, 1.0])
        w, h = box.estimate_pixel_dims(30.0)
        assert 3700 <= w <= 3730 and 3700 <= h <= 3730

    def test_method_non_positive_scale_raises(self):
        """The method also raises on a non-positive scale (delegates errors)."""
        box = SpatialExtent.from_pairs([0.0, 1.0], [0.0, 1.0])
        with pytest.raises(ValueError, match="scale_m must be positive"):
            box.estimate_pixel_dims(0.0)
