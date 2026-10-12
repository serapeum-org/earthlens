"""Tests for `OutputGrid` and the pure sizing functions behind it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.gee.grid import (
    NATIVE_CRS,
    OutputGrid,
    bbox_to_crs,
    geographic_grid,
    projected_grid,
    same_crs,
    same_epsg_code,
)

#: A small AOI over Cairo, the one the shipped doctests use.
CAIRO = (31.2, 29.9, 31.3, 30.0)

#: An orthographic CRS whose domain is one hemisphere, so an AOI on the far side
#: transforms to infinities. It is metre-based and projected, so it passes the
#: eligibility check and is the only honest way to reach the finiteness guards.
ORTHO = "+proj=ortho +lat_0=0 +lon_0=0 +datum=WGS84 +units=m"


class TestGeographicGrid:
    """The EEDAI convention: square metres on the ground."""

    def test_shortens_longitude_by_cos_latitude(self):
        """At 30 degrees north the column count is below the row count."""
        rows, cols = geographic_grid(CAIRO, 90.0)
        assert (rows, cols) == (124, 108)
        assert cols < rows, "longitude must be shortened away from the equator"

    def test_axes_match_at_the_equator(self):
        """A degree of longitude is barely shortened there."""
        assert geographic_grid((0.0, 0.0, 1.0, 1.0), 1000.0) == (112, 112)

    def test_a_sub_pixel_aoi_still_yields_one_pixel(self):
        """Rounding up never produces a zero-sized raster."""
        assert geographic_grid((31.2, 29.9, 31.2001, 29.9001), 90.0) == (1, 1)

    def test_uses_the_poleward_edge_not_the_mid_latitude(self):
        """A tall AOI must not under-count columns nearer the pole."""
        tall = (0.0, 50.0, 1.0, 70.0)
        _rows, cols = geographic_grid(tall, 1000.0)
        # The poleward edge (70 deg) shortens more than the mid-latitude (60 deg)
        # would, so the count is the finer of the two.
        import math

        at_mid = math.ceil(1.0 * 111_320.0 * math.cos(math.radians(60.0)) / 1000.0)
        assert cols < at_mid

    @pytest.mark.parametrize("scale", [0, -90.0, float("inf"), float("nan")])
    def test_rejects_a_non_positive_or_non_finite_scale(self, scale):
        """A bad scale fails here rather than as an opaque arithmetic error."""
        with pytest.raises(ValueError, match="scale"):
            geographic_grid(CAIRO, scale)

    def test_rejects_non_finite_bounds(self):
        """Infinities would reach `math.ceil` and raise an OverflowError."""
        with pytest.raises(ValueError, match="finite"):
            geographic_grid((float("inf"), 0.0, 1.0, 1.0), 90.0)

    def test_clamps_at_the_pole(self):
        """`cos` reaches zero at 90 degrees, which would divide by zero."""
        rows, cols = geographic_grid((0.0, 89.5, 1.0, 90.0), 1000.0)
        assert rows >= 1 and cols >= 1


class TestProjectedGrid:
    """A metre-based projected CRS sizes by span over scale directly."""

    def test_sizes_by_metre_span(self):
        """A 10 km by 10 km window at 100 m is a 100 by 100 grid."""
        assert projected_grid((0.0, 0.0, 10_000.0, 10_000.0), 100.0, "EPSG:32636") == (
            100,
            100,
        )

    def test_rejects_non_finite_bounds_naming_the_crs(self):
        """The message quotes the CRS, since that is what made them infinite."""
        with pytest.raises(ValueError, match="EPSG:32636"):
            projected_grid((float("inf"),) * 4, 100.0, "EPSG:32636")

    def test_rejects_a_non_positive_scale(self):
        """Zero scale would divide by zero."""
        with pytest.raises(ValueError, match="positive"):
            projected_grid((0.0, 0.0, 1.0, 1.0), 0, "EPSG:32636")


class TestTheTwoConventionsDisagree:
    """The reason this value object exists at all."""

    def test_metre_and_degree_grids_differ_away_from_the_equator(self):
        """Earth Engine reads `scale` as a degree-equivalent; the reader does not."""
        metres = OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0)
        degrees = OutputGrid.for_earth_engine(CAIRO, crs=NATIVE_CRS, scale=90.0)
        assert metres.cols != degrees.cols, (
            "the documented engine difference disappeared, which is the thing "
            "this object exists to keep visible"
        )
        assert metres.rows == degrees.rows, "latitude is not shortened either way"

    def test_both_conventions_agree_on_a_projected_crs(self):
        """With metres on both axes there is nothing to disagree about."""
        bbox = (0.0, 0.0, 10_000.0, 10_000.0)
        metres = OutputGrid.for_metre_grid(bbox, crs="EPSG:32636", scale=100.0)
        degrees = OutputGrid.for_earth_engine(bbox, crs="EPSG:32636", scale=100.0)
        assert metres.as_rows_cols() == degrees.as_rows_cols()

    def test_each_records_which_convention_sized_it(self):
        """A grid says how it was built, so a mismatch is diagnosable."""
        assert (
            OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0).convention
            == "metre"
        )
        assert (
            OutputGrid.for_earth_engine(CAIRO, crs=NATIVE_CRS, scale=90.0).convention
            == "degree"
        )


class TestFromCrsTransform:
    """An explicitly pinned grid, so stacked dates align without resampling."""

    def test_sizes_from_the_transform(self):
        """A 10 m transform over a 1 km window is a 100 by 100 grid."""
        grid = OutputGrid.from_crs_transform(
            (300_000.0, 3_400_000.0, 301_000.0, 3_401_000.0),
            crs="EPSG:32636",
            crs_transform=(10.0, 0.0, 300_000.0, 0.0, -10.0, 3_401_000.0),
        )
        assert grid.as_rows_cols() == (100, 100)
        assert grid.convention == "transform"
        assert grid.scale is None, "a pinned grid has no single metre scale"

    def test_a_negative_y_scale_is_accepted(self):
        """North-up transforms carry a negative y pixel size by convention."""
        grid = OutputGrid.from_crs_transform(
            (0.0, 0.0, 100.0, 100.0),
            crs="EPSG:32636",
            crs_transform=(10.0, 0.0, 0.0, 0.0, -10.0, 100.0),
        )
        assert grid.rows == 10

    def test_rejects_a_wrong_length_transform(self):
        """An affine transform is six numbers."""
        with pytest.raises(ValueError, match="6 elements"):
            OutputGrid.from_crs_transform(
                (0.0, 0.0, 1.0, 1.0), crs="EPSG:32636", crs_transform=(10.0, 0.0)
            )

    def test_rejects_a_zero_pixel_size(self):
        """A zero pixel size would divide by zero."""
        with pytest.raises(ValueError, match="non-zero"):
            OutputGrid.from_crs_transform(
                (0.0, 0.0, 1.0, 1.0),
                crs="EPSG:32636",
                crs_transform=(0.0, 0.0, 0.0, 0.0, -10.0, 1.0),
            )


class TestBudgets:
    """`fits` and `tiles`, which the routing decision is made on."""

    def test_a_grid_within_both_budgets_fits(self):
        """The common case returns no reason."""
        grid = OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0)
        assert grid.fits(max_pixels=200_000_000, max_dimension=32768) == (True, "")

    def test_the_per_axis_cap_is_reported_first(self):
        """A long thin window trips the axis cap, not the pixel budget."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=1, cols=40_000)
        fits, reason = grid.fits(max_pixels=200_000_000, max_dimension=32768)
        assert fits is False
        assert "longest axis" in reason

    def test_the_pixel_budget_counts_bands(self):
        """Three bands of a grid that fits alone can exceed the budget together."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=10_000, cols=10_000)
        assert grid.fits(max_pixels=150_000_000, max_dimension=32768)[0] is True
        fits, reason = grid.fits(
            max_pixels=150_000_000, max_dimension=32768, band_count=3
        )
        assert fits is False
        assert "10000x10000" in reason

    def test_band_pixels_treats_zero_bands_as_one(self):
        """A bandless request still reads one plane of pixels."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=10, cols=10)
        assert grid.band_pixels(0) == 100

    def test_tiles_counts_both_axes(self):
        """A grid 1.5 tiles wide and 2.5 tall cuts into 2 by 3 tiles."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=5120, cols=3072)
        assert grid.tiles(2048) == 6

    def test_a_single_pass_is_one_tile(self):
        """A grid smaller than one tile is not zero tiles."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=10, cols=10)
        assert grid.tiles(2048) == 1

    def test_tiles_rejects_a_non_positive_size(self):
        """A zero tile size would divide by zero."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=10, cols=10)
        with pytest.raises(ValueError, match="positive"):
            grid.tiles(0)

    def test_longest_axis_and_pixels(self):
        """The two derived sizes the budgets read."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=3, cols=7)
        assert (grid.longest_axis, grid.pixels) == (7, 21)


class TestValueSemantics:
    """It is a value object, not a mutable bag."""

    def test_is_frozen(self):
        """A grid the routing decision used cannot be edited afterwards."""
        grid = OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0)
        with pytest.raises(ValidationError):
            grid.rows = 1

    def test_rejects_an_unknown_field(self):
        """A mistyped keyword fails at construction."""
        with pytest.raises(ValidationError):
            OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=1, cols=1, columns=2)

    def test_summary_reads_as_one_line(self):
        """It appears in log lines and plans, so it must stay readable."""
        grid = OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0)
        text = str(grid)
        assert text.startswith("OutputGrid(")
        assert "EPSG:4326" in text and "metre" in text

    def test_carries_the_cutline_alongside_the_bbox(self):
        """The reader takes both: bbox sizes the grid, cutline trims the output."""
        sentinel = object()
        grid = OutputGrid.for_metre_grid(
            CAIRO, crs=NATIVE_CRS, scale=90.0, cutline=sentinel
        )
        assert grid.cutline is sentinel


class TestCrsHelpers:
    """The CRS comparison and reprojection the grid is built on."""

    def test_an_equivalent_proj_string_matches(self):
        """PROJ decides equality, so a UTM PROJ string is EPSG:32636."""
        from pyproj import CRS

        assert same_crs(
            CRS.from_user_input("+proj=utm +zone=36 +datum=WGS84"), "EPSG:32636"
        )

    def test_a_different_crs_does_not_match(self):
        """The same comparison must still warp a genuinely different CRS."""
        from pyproj import CRS

        assert not same_crs(CRS.from_user_input("EPSG:4326"), "EPSG:32636")

    def test_a_non_epsg_authority_naming_the_same_crs_matches(self):
        """`ESRI:102100` and `EPSG:3857` are one CRS."""
        from pyproj import CRS

        assert same_crs(CRS.from_user_input("ESRI:102100"), "EPSG:3857")

    def test_an_unparseable_target_falls_back_to_the_code_check(self):
        """PROJ cannot read `ESRI:3857`, so the EPSG shortcut must decline it."""

        class _Crs:
            """A CRS object exposing only `to_epsg`, as a bare fake would."""

            def to_epsg(self):
                return 3857

        assert not same_crs(_Crs(), "ESRI:3857")

    def test_the_code_fallback_trusts_only_an_epsg_target(self):
        """A matching number under another authority is not a match."""

        class _Crs:
            """A CRS object exposing only `to_epsg`."""

            def to_epsg(self):
                return 3857

        assert same_epsg_code(_Crs(), "EPSG:3857")
        assert not same_epsg_code(_Crs(), "ESRI:3857")

    def test_bbox_passes_through_unchanged_under_4326(self):
        """No reprojection is needed when the target is already lat/lon."""
        assert bbox_to_crs(CAIRO, NATIVE_CRS) == CAIRO

    def test_bbox_reprojects_into_a_projected_crs(self):
        """A UTM window comes back in metres, not degrees."""
        out = bbox_to_crs(CAIRO, "EPSG:32636")
        assert min(out) > 1000, "a UTM easting/northing is far larger than a degree"

    def test_an_aoi_outside_the_projection_yields_non_finite_bounds(self):
        """The case the finiteness guards exist for, reached honestly."""
        import math

        out = bbox_to_crs((150.0, -10.0, 160.0, 10.0), ORTHO)
        assert not all(map(math.isfinite, out))

    def test_that_aoi_is_refused_by_name(self):
        """A grid cannot be sized for it, and the error says why."""
        bbox = bbox_to_crs((150.0, -10.0, 160.0, 10.0), ORTHO)
        with pytest.raises(ValueError, match="finite"):
            OutputGrid.for_metre_grid(bbox, crs=ORTHO, scale=90.0)
