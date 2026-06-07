"""Unit tests for `earthlens.grids` (HEALPix / octahedral / ORCA adapters)."""

from __future__ import annotations

import numpy as np
import pytest

from earthlens.grids import from_healpix, from_octahedral, from_orca


class TestFromHealpix:
    """`from_healpix` regrids a HEALPix field via the scattered-point bridge."""

    def test_ring_nside1_returns_single_band_4326(self):
        """A 12-pixel RING field yields a single-band EPSG:4326 raster."""
        ds = from_healpix(np.arange(12.0), cell_size=30.0)
        assert ds.band_count == 1
        assert ds.epsg == 4326

    def test_nside_is_derived_from_length(self):
        """An omitted nside is inferred from the pixel count."""
        ds = from_healpix(np.arange(48.0), cell_size=30.0)
        assert ds.band_count == 1

    def test_nested_ordering_supported(self):
        """NESTED ordering with a power-of-two nside regrids successfully."""
        ds = from_healpix(np.arange(48.0), nside=2, nest=True, cell_size=20.0)
        assert ds.band_count == 1

    def test_invalid_pixel_count_raises(self):
        """A length that is not 12*nside**2 is rejected."""
        with pytest.raises(ValueError, match="valid HEALPix pixel count"):
            from_healpix(np.zeros(10), cell_size=30.0)

    def test_nside_disagrees_with_length_raises(self):
        """An explicit nside inconsistent with the length is rejected."""
        with pytest.raises(ValueError, match="valid HEALPix pair"):
            from_healpix(np.zeros(12), nside=2, cell_size=30.0)

    def test_nested_non_power_of_two_raises(self):
        """NESTED ordering requires a power-of-two nside."""
        with pytest.raises(ValueError, match="power of two"):
            from_healpix(np.arange(108.0), nside=3, nest=True, cell_size=20.0)


class TestFromOctahedral:
    """`from_octahedral` regrids ragged per-point fields."""

    def test_four_corners_shape(self):
        """Four corner points produce a 5x5 single-band raster at cell_size 1."""
        lats = np.array([0.0, 0.0, 5.0, 5.0])
        lons = np.array([0.0, 5.0, 0.0, 5.0])
        values = np.array([1.0, 2.0, 3.0, 4.0])
        ds = from_octahedral(lats, lons, values, cell_size=1.0, algorithm="nearest")
        assert (ds.rows, ds.columns, ds.band_count) == (5, 5, 1)

    def test_unequal_length_raises(self):
        """Coordinate/value arrays of unequal length are rejected."""
        with pytest.raises(ValueError, match="equal length"):
            from_octahedral(np.zeros(4), np.zeros(3), np.zeros(4), cell_size=1.0)


class TestFromOrca:
    """`from_orca` regrids curvilinear (ny, nx) fields via the mesh bridge."""

    def test_small_field_returns_single_band_4326(self):
        """A 2x3 curvilinear field yields a single-band EPSG:4326 raster."""
        lon2d = np.array([[0.0, 1.0, 2.0], [0.0, 1.0, 2.0]])
        lat2d = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]])
        data2d = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        ds = from_orca(lon2d, lat2d, data2d, cell_size=0.5)
        assert ds.band_count == 1
        assert ds.epsg == 4326

    def test_mismatched_shapes_raise(self):
        """Coordinate/data arrays of differing shape are rejected."""
        with pytest.raises(ValueError, match="same shape"):
            from_orca(np.zeros((2, 3)), np.zeros((2, 2)), np.zeros((2, 3)), cell_size=0.5)

    def test_non_2d_input_raises(self):
        """1-D inputs are rejected (ORCA needs a 2-D mesh)."""
        with pytest.raises(ValueError, match="2-D"):
            from_orca(np.zeros(4), np.zeros(4), np.zeros(4), cell_size=0.5)

    def test_too_small_grid_raises(self):
        """A grid smaller than 2x2 cannot form quad cells."""
        with pytest.raises(ValueError, match="at least 2 x 2"):
            from_orca(np.zeros((1, 3)), np.zeros((1, 3)), np.zeros((1, 3)), cell_size=0.5)
