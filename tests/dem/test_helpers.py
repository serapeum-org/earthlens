"""Unit tests for `earthlens.dem._helpers`: pure tile-key arithmetic."""

from __future__ import annotations

import pytest

from earthlens.dem._helpers import Tile, bbox_to_tiles, tile_key, tile_name

pytestmark = [pytest.mark.dem, pytest.mark.unit]


class TestTileNameFormatting:
    """Verify the Copernicus tile-name convention pinned by the A1 gate."""

    def test_northern_hemisphere_positive_lon(self):
        """A land tile in Egypt (30N, 31E) formats as `N30_00_E031_00`."""
        assert (
            tile_name(Tile(lat=30, lon=31), "10")
            == "Copernicus_DSM_COG_10_N30_00_E031_00_DEM"
        )

    def test_southern_hemisphere_negative_lon(self):
        """A South American tile (-15S, -47W) uses `S15_00_W047_00`."""
        assert (
            tile_name(Tile(lat=-15, lon=-47), "30")
            == "Copernicus_DSM_COG_30_S15_00_W047_00_DEM"
        )

    def test_equator_and_prime_meridian_zero_pad(self):
        """The (0, 0) tile pads with two-digit lat and three-digit lon."""
        assert (
            tile_name(Tile(lat=0, lon=0), "10")
            == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"
        )

    def test_tile_key_appends_tif(self):
        """The bucket key is `<name>/<name>.tif`."""
        assert (
            tile_key(Tile(lat=30, lon=31), "10")
            == "Copernicus_DSM_COG_10_N30_00_E031_00_DEM/"
            "Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif"
        )


class TestBboxToTiles:
    """Verify the bbox-to-tile enumeration on the integer-degree grid."""

    def test_single_tile_bbox(self):
        """A sub-degree bbox lands entirely on one tile."""
        tiles = bbox_to_tiles(30.2, 30.8, 31.2, 31.8)
        assert [(t.lat, t.lon) for t in tiles] == [(30, 31)]

    def test_multi_tile_bbox(self):
        """A bbox spanning two tiles east-west returns both."""
        tiles = bbox_to_tiles(0.5, 0.5, 5.5, 7.5)
        assert sorted((t.lat, t.lon) for t in tiles) == [(0, 5), (0, 6), (0, 7)]

    def test_equator_prime_meridian_tile(self):
        """A bbox crossing (0, 0) picks up the (0, 0) tile."""
        tiles = bbox_to_tiles(-0.4, 0.4, -0.4, 0.4)
        assert sorted((t.lat, t.lon) for t in tiles) == [
            (-1, -1),
            (-1, 0),
            (0, -1),
            (0, 0),
        ]

    def test_negative_lat_lon(self):
        """A southern-hemisphere / western-hemisphere bbox uses S/W tiles."""
        tiles = bbox_to_tiles(-15.5, -14.5, -47.5, -46.5)
        assert sorted((t.lat, t.lon) for t in tiles) == [
            (-16, -48),
            (-16, -47),
            (-15, -48),
            (-15, -47),
        ]

    def test_antimeridian_bbox_rejected(self):
        """A `lon_min > lon_max` bbox is rejected — antimeridian split is out of scope."""
        with pytest.raises(ValueError, match="antimeridian-straddling"):
            bbox_to_tiles(0.4, 0.6, 179.6, -179.6)

    def test_exact_degree_upper_boundary_excludes_next_tile(self):
        """A bbox whose upper edge lands on an integer degree stops there."""
        # bbox `[0.0, 2.0] x [0.0, 2.0]` covers the 2x2 tiles at
        # `[0, 1] x [0, 1]`; tile 2 covers `[2, 3)`, which the bbox does
        # not reach, so 3x3 = 9 tiles would be one too many per axis.
        tiles = bbox_to_tiles(0.0, 2.0, 0.0, 2.0)
        assert sorted((t.lat, t.lon) for t in tiles) == [
            (0, 0),
            (0, 1),
            (1, 0),
            (1, 1),
        ]

    def test_zero_width_bbox_returns_single_tile(self):
        """A zero-width bbox on an integer boundary still returns that tile."""
        tiles = bbox_to_tiles(0.0, 0.0, 0.0, 0.0)
        assert [(t.lat, t.lon) for t in tiles] == [(0, 0)]

    def test_whole_earth_row_major(self):
        """A whole-Earth bbox yields 180 x 360 tiles in row-major order."""
        tiles = bbox_to_tiles(-90.0, 90.0, -180.0, 180.0)
        assert len(tiles) == 180 * 360
        # First tile is the (-90, -180) corner, last is (89, 179).
        assert (tiles[0].lat, tiles[0].lon) == (-90, -180)
        assert (tiles[-1].lat, tiles[-1].lon) == (89, 179)

    def test_inverted_lat_raises(self):
        """A `lat_min > lat_max` bbox is rejected."""
        with pytest.raises(ValueError, match="lat_min"):
            bbox_to_tiles(10.0, 5.0, 0.0, 1.0)

    def test_out_of_range_lat_raises(self):
        """A latitude outside `[-90, 90]` is rejected."""
        with pytest.raises(ValueError, match="latitude"):
            bbox_to_tiles(-91.0, 0.0, 0.0, 1.0)

    def test_out_of_range_lon_raises(self):
        """A longitude outside `[-180, 180]` is rejected."""
        with pytest.raises(ValueError, match="longitude"):
            bbox_to_tiles(0.0, 1.0, -181.0, 0.0)
