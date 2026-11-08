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


class _EpsgOnlyCrs:
    """A CRS object exposing only `to_epsg`, as a bare fake would."""

    def __init__(self, code: int = 3857) -> None:
        self._code = code

    def to_epsg(self) -> int:
        """Return the EPSG code this stand-in reports."""
        return self._code


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

    def test_a_tall_aoi_honours_the_requested_scale(self):
        """Sized on the equatorward edge, so no pixel is coarser than asked."""
        # 72 columns over 1 degree of longitude is 994 m per pixel at latitude
        # 50, the widest point of the AOI, for a requested 1000 m. Sizing on the
        # poleward edge gave 39 columns, i.e. 1835 m - coarser than asked.
        assert geographic_grid((0.0, 50.0, 1.0, 70.0), 1000.0) == (2227, 72)

    def test_a_southern_aoi_is_sized_the_same_as_its_mirror(self):
        """The edge is chosen by distance from the equator, not by sign."""
        northern = geographic_grid((0.0, 50.0, 1.0, 70.0), 1000.0)
        southern = geographic_grid((0.0, -70.0, 1.0, -50.0), 1000.0)
        assert northern == southern

    def test_a_global_aoi_spans_the_full_equator(self):
        """An AOI containing the equator is not shortened at all."""
        import math

        rows, cols = geographic_grid((-180.0, -90.0, 180.0, 90.0), 1000.0)
        assert cols == math.ceil(360 * 111_320.0 / 1000.0)
        assert rows == math.ceil(180 * 111_320.0 / 1000.0)

    def test_an_aoi_straddling_the_equator_uses_the_equator(self):
        """`min(|min_y|, |max_y|)` would pick the wrong edge here."""
        # The equator is inside the span, so cos is 1 and the full width is kept.
        assert geographic_grid((0.0, -10.0, 1.0, 10.0), 1000.0)[1] == 112

    def test_an_aoi_inside_the_polar_cap_still_yields_a_pixel(self):
        """The clamp keeps cos away from zero at the pole itself."""
        assert geographic_grid((0.0, 89.95, 1.0, 89.99), 1000.0) == (5, 1)

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
        assert rows >= 1
        assert cols >= 1


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

    def test_rejects_a_negative_scale(self):
        """A negative scale would invert the grid rather than divide by zero."""
        with pytest.raises(ValueError, match="positive"):
            projected_grid((0.0, 0.0, 1.0, 1.0), -100.0, "EPSG:32636")


class TestForEarthEngine:
    """The degree-equivalent convention, and the guards it applies itself."""

    def test_sizes_both_axes_by_the_same_degree_step(self):
        """A square-degree AOI comes out square, which is the whole difference."""
        assert OutputGrid.for_earth_engine(
            CAIRO, crs=NATIVE_CRS, scale=90.0
        ).as_rows_cols() == (124, 124)

    def test_rejects_non_finite_bounds(self):
        """Infinities would reach `math.ceil` and raise an OverflowError."""
        with pytest.raises(ValueError, match="finite"):
            OutputGrid.for_earth_engine(
                (float("inf"), 0.0, 1.0, 1.0), crs=NATIVE_CRS, scale=90.0
            )

    @pytest.mark.parametrize("scale", [0, -90.0, float("inf"), float("nan")])
    def test_rejects_a_non_positive_or_non_finite_scale(self, scale):
        """A bad scale fails here rather than as an opaque arithmetic error."""
        with pytest.raises(ValueError, match="scale"):
            OutputGrid.for_earth_engine(CAIRO, crs=NATIVE_CRS, scale=scale)

    def test_a_sub_degree_aoi_still_yields_one_pixel(self):
        """Rounding up never produces a zero-sized raster."""
        tiny = (31.2, 29.9, 31.20001, 29.90001)
        assert OutputGrid.for_earth_engine(
            tiny, crs=NATIVE_CRS, scale=90.0
        ).as_rows_cols() == (1, 1)

    def test_a_projected_crs_delegates_to_the_metre_sizing(self):
        """Away from a geographic CRS there is no degree step to apply."""
        bbox = (0.0, 0.0, 10_000.0, 10_000.0)
        grid = OutputGrid.for_earth_engine(bbox, crs="EPSG:32636", scale=100.0)
        assert grid.as_rows_cols() == projected_grid(bbox, 100.0, "EPSG:32636")


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

    def test_it_carries_the_transform_it_was_built_from(self):
        """A pinned grid has to be able to hand the transform to a request."""
        terms = (10.0, 0.0, 300_000.0, 0.0, -10.0, 3_401_000.0)
        grid = OutputGrid.from_crs_transform(
            (300_000.0, 3_400_000.0, 301_000.0, 3_401_000.0),
            crs="EPSG:32636",
            crs_transform=terms,
        )
        assert grid.crs_transform == terms

    def test_integer_terms_are_normalised_to_floats(self):
        """So a caller passing ints and one passing floats produce one value."""
        grid = OutputGrid.from_crs_transform(
            (0.0, 0.0, 100.0, 100.0),
            crs="EPSG:32636",
            crs_transform=(10, 0, 0, 0, -10, 100),
        )
        assert grid.crs_transform == (10.0, 0.0, 0.0, 0.0, -10.0, 100.0)

    def test_the_sized_constructors_pin_nothing(self):
        """The field is what tells a pinned grid from a scale-sized one."""
        assert (
            OutputGrid.for_metre_grid(CAIRO, crs=NATIVE_CRS, scale=90.0).crs_transform
            is None
        )
        assert (
            OutputGrid.for_earth_engine(CAIRO, crs=NATIVE_CRS, scale=90.0).crs_transform
            is None
        )

    def test_a_negative_y_scale_is_accepted(self):
        """North-up transforms carry a negative y pixel size by convention."""
        grid = OutputGrid.from_crs_transform(
            (0.0, 0.0, 100.0, 100.0),
            crs="EPSG:32636",
            crs_transform=(10.0, 0.0, 0.0, 0.0, -10.0, 100.0),
        )
        assert grid.rows == 10

    def test_a_positive_y_scale_is_normalised_to_north_up(self):
        """A south-up transform must not invert the grid it sizes.

        The terms are accepted, so the only safe reading is the north-up one:
        the same window, the same pixel count, and a negative `y_scale` with the
        origin at the top so the request cannot place the raster upside down.
        """
        grid = OutputGrid.from_crs_transform(
            (0.0, 0.0, 100.0, 100.0),
            crs="EPSG:32636",
            crs_transform=(10.0, 0.0, 0.0, 0.0, 10.0, 0.0),
        )
        assert grid.as_rows_cols() == (10, 10)
        assert grid.bbox == (0.0, 0.0, 100.0, 100.0)
        assert grid.crs_transform == (10.0, 0.0, 0.0, 0.0, -10.0, 100.0)

    def test_a_window_below_the_pinned_origin_snaps_outward(self):
        """Snapping has to floor, not truncate, or a negative offset rounds in.

        An AOI south and west of the pinned origin gives both offsets a negative
        sign, which is where `int()` would snap towards the origin and clip the
        window the caller asked for.
        """
        grid = OutputGrid.from_crs_transform(
            (-70.045, -33.515, -70.015, -33.485),
            crs=NATIVE_CRS,
            crs_transform=(0.01, 0.0, -70.0, 0.0, -0.01, -33.0),
        )
        assert grid.bbox[0] <= -70.045
        assert grid.bbox[3] >= -33.485
        offset = (grid.bbox[0] - (-70.0)) / 0.01
        assert offset == pytest.approx(round(offset), abs=1e-6)

    def test_a_degenerate_window_still_gets_one_pixel(self):
        """A zero-area window would otherwise size a grid with no extent."""
        grid = OutputGrid.from_crs_transform(
            (300_000.0, 3_400_000.0, 300_000.0, 3_400_000.0),
            crs="EPSG:32636",
            crs_transform=(10.0, 0.0, 300_000.0, 0.0, -10.0, 3_400_000.0),
        )
        assert grid.as_rows_cols() == (1, 1)
        assert grid.bbox == (300_000.0, 3_399_990.0, 300_010.0, 3_400_000.0)

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

    def test_the_per_axis_cap_is_inclusive(self):
        """A grid exactly at the cap is within it, not over it."""
        at_cap = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=1, cols=32_768)
        over = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=1, cols=32_769)
        assert at_cap.fits(max_pixels=10**12, max_dimension=32_768)[0] is True
        assert over.fits(max_pixels=10**12, max_dimension=32_768)[0] is False

    def test_the_pixel_budget_is_inclusive(self):
        """So is the total-pixel budget, which the plan is sized against."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=100, cols=100)
        assert grid.fits(max_pixels=10_000, max_dimension=32_768) == (True, "")
        assert grid.fits(max_pixels=9_999, max_dimension=32_768)[0] is False

    def test_tiles_rejects_a_negative_size(self):
        """A negative tile side is as meaningless as a zero one."""
        grid = OutputGrid(bbox=(0, 0, 1, 1), crs=NATIVE_CRS, rows=10, cols=10)
        with pytest.raises(ValueError, match="positive"):
            grid.tiles(-2048)


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
        assert "EPSG:4326" in text
        assert "metre" in text

    @pytest.mark.parametrize("crs", ["epsg:4326", "EPSG:4326"])
    def test_the_crs_dispatch_is_case_insensitive(self, crs):
        """A lowercase code must not fall through to the projected sizing."""
        assert OutputGrid.for_metre_grid(CAIRO, crs=crs, scale=90.0).as_rows_cols() == (
            124,
            108,
        )
        assert OutputGrid.for_earth_engine(
            CAIRO, crs=crs, scale=90.0
        ).as_rows_cols() == (124, 124)

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

    def test_the_code_fallback_rejects_a_non_numeric_code(self):
        """A code that is not digits must decline rather than fail in `int`."""
        assert not same_epsg_code(_EpsgOnlyCrs(), "EPSG:not-a-code")

    def test_the_code_fallback_needs_something_to_ask(self):
        """A CRS with no `to_epsg` cannot answer, so reprojecting is the answer."""
        assert not same_epsg_code("EPSG:3857", "EPSG:3857")

    def test_the_code_fallback_compares_the_numbers(self):
        """A mismatched code is not a match either."""
        assert not same_epsg_code(_EpsgOnlyCrs(3857), "EPSG:4326")

    def test_bbox_passes_through_a_lowercase_native_crs(self):
        """The dispatch upper-cases, so `epsg:4326` needs no transformer."""
        assert bbox_to_crs(CAIRO, "epsg:4326") == CAIRO
