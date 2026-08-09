"""Unit tests for the JRC-flood URL + pixel-window helpers (no network)."""

from __future__ import annotations

import pytest

from earthlens.jrc_flood import _helpers as h

pytestmark = pytest.mark.jrc_flood

#: The verified EFHM geotransform (EPSG:4326, ~3 arc-second) + raster size.
_GT = (-24.54208333, 0.0008333333333333334, 0.0, 71.13375, 0.0, -0.0008333333333333334)
_COLUMNS = 110162
_ROWS = 51992


class TestEfhmUrl:
    """Tests for efhm_url."""

    def test_default_url(self):
        """efhm_url builds the verified RP100 URL."""
        assert h.efhm_url(100) == (
            "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/"
            "flood_hazard/Europe_RP100_filled_depth.tif"
        )

    def test_custom_base_and_template(self):
        """A custom base + template are honoured."""
        assert h.efhm_url(50, base_url="http://x", template="rp{rp}.tif") == (
            "http://x/rp50.tif"
        )


class TestPixelWindow:
    """Tests for pixel_window."""

    def test_small_aoi_window(self):
        """A small AOI maps to a small clamped window."""
        assert h.pixel_window(_GT, (4.8, 51.8, 5.0, 52.0), _COLUMNS, _ROWS) == (
            35210,
            22960,
            241,
            241,
        )

    def test_outside_coverage_returns_none(self):
        """An AOI south of the coverage returns None."""
        assert h.pixel_window(_GT, (4.8, -5.0, 5.0, -4.8), _COLUMNS, _ROWS) is None

    def test_west_of_coverage_returns_none(self):
        """An AOI west of the raster origin returns None."""
        assert h.pixel_window(_GT, (-40.0, 40.0, -39.0, 41.0), _COLUMNS, _ROWS) is None

    def test_clamps_to_raster_extent(self):
        """A window overflowing the north-west corner clamps to the raster."""
        window = h.pixel_window(_GT, (-30.0, 68.0, -20.0, 75.0), _COLUMNS, _ROWS)
        assert window is not None
        col_off, row_off, cols, rows = window
        assert col_off == 0 and row_off == 0
        assert 0 < cols <= _COLUMNS and 0 < rows <= _ROWS


class TestWindowOrigin:
    """Tests for window_origin."""

    def test_shifts_origin_keeps_pixel_size(self):
        """window_origin shifts the origin by the offset, keeping pixel sizes."""
        geo = h.window_origin(_GT, 35210, 22960)
        assert geo[1] == _GT[1] and geo[5] == _GT[5]
        assert geo[0] == pytest.approx(_GT[0] + 35210 * _GT[1])
        assert geo[3] == pytest.approx(_GT[3] + 22960 * _GT[5])
