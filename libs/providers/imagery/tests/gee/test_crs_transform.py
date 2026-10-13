"""Tests for `crs_transform=`, the explicitly pinned output grid.

`OutputGrid.from_crs_transform` shipped with Epic 1 as the third named
constructor and nothing called it. These tests cover the kwarg that gives it a
caller: validation at construction, the grid it produces, the three Earth Engine
request shapes that carry it, and the one engine that cannot honour it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.gee import backend as backend_module
from earthlens.gee.pixels import PixelsEngine

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"

#: A north-up grid of 0.001-degree pixels pinned to a round lat/lon origin.
PINNED = (0.001, 0.0, 31.2, 0.0, -0.001, 30.0)

START = dt.datetime(2000, 2, 11)
END = dt.datetime(2000, 2, 13)


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read."""
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    return make_gee(**params)


def _composited(gee):
    """The `(var_info, image)` pair the export path would be handed."""
    var_info = gee.catalog.get_dataset(ASSET)
    collection = gee._build_collection(var_info, ["elevation"], START, END)
    ((_when, image, _start, _end),) = gee._composite(collection, var_info, START, END)
    return var_info, image


def _collect_warnings(monkeypatch):
    """Collect the backend's warnings instead of emitting them."""
    collected: list[str] = []
    monkeypatch.setattr(
        backend_module.logger,
        "warning",
        lambda msg, *args, **kwargs: collected.append(msg),
    )
    return collected


def _export(gee):
    """Run one bucket through `_api` and return the image it exported."""
    var_info, image = _composited(gee)
    gee._api(image, var_info, ["elevation"], START, START, END)
    return image


class TestValidation:
    """Refused at construction, before the catalog parse or any request."""

    def test_unpinned_by_default(self, make_gee, fake_reader):
        """The feature is opt-in; nothing changes until it is asked for."""
        assert _gee(make_gee).crs_transform is None

    def test_a_valid_transform_is_kept_as_floats(self, make_gee, fake_reader):
        """Normalised once, so every reader downstream sees the same numbers."""
        gee = _gee(make_gee, crs_transform=[1, 0, 31, 0, -1, 30])
        assert gee.crs_transform == (1.0, 0.0, 31.0, 0.0, -1.0, 30.0)

    @pytest.mark.parametrize("bad", ["0.001,0,31", b"\x00", 90.0, None.__class__])
    def test_a_non_iterable_is_refused(self, make_gee, fake_reader, bad):
        """A string iterates, which is exactly the trap worth closing."""
        with pytest.raises(TypeError, match="six numbers"):
            _gee(make_gee, crs_transform=bad)

    def test_a_non_numeric_entry_is_refused(self, make_gee, fake_reader):
        """Six entries, but not six numbers."""
        with pytest.raises(TypeError, match="six numbers"):
            _gee(make_gee, crs_transform=[0.001, 0, "x", 0, -0.001, 30])

    @pytest.mark.parametrize("length", [5, 7])
    def test_the_wrong_length_is_refused(self, make_gee, fake_reader, length):
        """Earth Engine's transform is six terms, not an arbitrary affine."""
        with pytest.raises(ValueError, match="6 elements"):
            _gee(make_gee, crs_transform=[1.0] * length)

    def test_a_non_finite_term_is_refused(self, make_gee, fake_reader):
        """A nan origin would size a grid of nan pixels."""
        with pytest.raises(ValueError, match="finite"):
            _gee(make_gee, crs_transform=(0.001, 0.0, float("nan"), 0.0, -0.001, 30.0))

    @pytest.mark.parametrize("index", [0, 4])
    def test_a_zero_pixel_size_is_refused(self, make_gee, fake_reader, index):
        """Either zero collapses the grid to no extent."""
        terms = list(PINNED)
        terms[index] = 0.0
        with pytest.raises(ValueError, match="non-zero"):
            _gee(make_gee, crs_transform=terms)


class TestTheGridIsPinned:
    """The transform sizes the grid, in place of `scale`."""

    def test_the_backend_builds_one(self, make_gee, fake_reader):
        """`_pinned_grid` is the single builder every path reads."""
        grid = _gee(make_gee, crs_transform=PINNED)._pinned_grid()
        assert grid is not None
        assert grid.convention == "transform"

    def test_an_unpinned_backend_builds_none(self, make_gee, fake_reader):
        """So every caller can tell "pinned" from "sized from scale"."""
        assert _gee(make_gee)._pinned_grid() is None

    def test_the_grid_carries_the_transform(self, make_gee, fake_reader):
        """Discarding it would leave nothing for a request to quote."""
        grid = _gee(make_gee, crs_transform=PINNED)._pinned_grid()
        assert grid.crs_transform == PINNED

    def test_the_pinned_grid_has_no_scale(self, make_gee, fake_reader):
        """Earth Engine treats the two as an either/or, and so does the grid."""
        assert _gee(make_gee, crs_transform=PINNED)._pinned_grid().scale is None

    def test_the_transform_sizes_the_pixels(self, make_gee, fake_reader):
        """0.001-degree pixels over the 0.1-degree AOI: a ~100-px side.

        101 and not 100 because the span is a float hair over 0.1 degrees and
        the grid ceilings rather than truncating, which is the right way round:
        a short grid would clip the AOI.
        """
        grid = _gee(make_gee, crs_transform=PINNED)._pinned_grid()
        assert (grid.cols, grid.rows) == (101, 101)

    def test_the_pixel_engine_reads_the_pinned_grid(self, make_gee, fake_reader):
        """One builder, so the engine cannot disagree with the backend."""
        gee = _gee(make_gee, engine="pixels", crs_transform=PINNED)
        assert PixelsEngine().grid_for(gee, 90.0).convention == "transform"

    def test_the_scale_no_longer_moves_the_grid(self, make_gee, fake_reader):
        """A pinned request is pinned: the scale argument stops mattering."""
        gee = _gee(make_gee, engine="pixels", crs_transform=PINNED)
        engine = PixelsEngine()
        assert engine.grid_for(gee, 900.0) == engine.grid_for(gee, 90.0)

    def test_an_unpinned_request_still_follows_the_scale(self, make_gee, fake_reader):
        """The default sizing is untouched by the feature."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        assert engine.grid_for(gee, 900.0).pixels < engine.grid_for(gee, 90.0).pixels


class TestTheRequestsCarryIt:
    """Each Earth Engine path sends the transform, and no scale beside it."""

    def test_the_pixel_api_sends_an_affine_transform(self, make_gee, fake_reader):
        """`computePixels` takes it as the grid's `affineTransform`."""
        gee = _gee(make_gee, engine="pixels", crs_transform=PINNED)
        _export(gee)
        (body,) = gee.client.compute_pixels_calls
        assert body["grid"]["affineTransform"] == {
            "scaleX": 0.001,
            "shearX": 0.0,
            "translateX": 31.2,
            "shearY": 0.0,
            "scaleY": -0.001,
            "translateY": 30.0,
        }

    def test_the_pixel_grid_keeps_its_dimensions(self, make_gee, fake_reader):
        """Both, not either: the transform places the pixels, dimensions count them."""
        gee = _gee(make_gee, engine="pixels", crs_transform=PINNED)
        grid = gee._pinned_grid()
        _export(gee)
        (body,) = gee.client.compute_pixels_calls
        assert body["grid"]["dimensions"] == {"width": grid.cols, "height": grid.rows}

    def test_an_unpinned_request_carries_a_derived_transform(
        self, make_gee, fake_reader
    ):
        """Dimensions alone are not a grid.

        Earth Engine falls back to the CRS's default transform, which returned
        this 90 m AOI over Cairo as 1-degree pixels starting at lat/lon 0 - a
        file that was written, was not empty, and held the wrong place. So an
        unpinned request has to describe its own window too.
        """
        gee = _gee(make_gee, engine="pixels")
        grid = PixelsEngine().grid_for(gee, 90.0)
        _export(gee)
        (body,) = gee.client.compute_pixels_calls
        terms = body["grid"]["affineTransform"]
        assert (terms["translateX"], terms["translateY"]) == (
            grid.bbox[0],
            grid.bbox[3],
        )
        assert terms["scaleY"] < 0, "north-up, so the y pixel size is negative"
        assert (terms["shearX"], terms["shearY"]) == (0.0, 0.0)

    def test_the_derived_transform_spans_the_window_exactly(
        self, make_gee, fake_reader
    ):
        """It divides the bbox rather than overhanging it by the ceiling."""
        gee = _gee(make_gee, engine="pixels")
        grid = PixelsEngine().grid_for(gee, 90.0)
        terms = grid.request_transform()
        assert terms[2] + terms[0] * grid.cols == pytest.approx(grid.bbox[2])
        assert terms[5] + terms[4] * grid.rows == pytest.approx(grid.bbox[1])

    def test_get_download_url_sends_it_instead_of_the_scale(
        self, make_gee, fake_reader
    ):
        """Earth Engine documents `crs_transform` and `scale` as exclusive."""
        gee = _gee(make_gee, engine="ee", crs_transform=PINNED)
        params = _export(gee).download_params
        assert params["crs_transform"] == list(PINNED)
        assert "scale" not in params

    def test_an_unpinned_url_request_still_sends_the_scale(self, make_gee, fake_reader):
        """The historical request is byte-for-byte what it was."""
        params = _export(_gee(make_gee, engine="ee")).download_params
        assert params["scale"] == 90.0
        assert "crs_transform" not in params

    def test_the_batch_export_sends_it_instead_of_the_scale(
        self, make_gee, fake_reader
    ):
        """`crsTransform` there, same exclusivity."""
        gee = _gee(
            make_gee, export_via="drive", drive_folder="out", crs_transform=PINNED
        )
        _export(gee)
        (_method, kwargs) = gee.client.export_image.calls[-1]
        assert kwargs["crsTransform"] == list(PINNED)
        assert "scale" not in kwargs

    def test_an_unpinned_batch_export_still_sends_the_scale(
        self, make_gee, fake_reader
    ):
        """Again, no change for the unpinned case."""
        gee = _gee(make_gee, export_via="drive", drive_folder="out")
        _export(gee)
        (_method, kwargs) = gee.client.export_image.calls[-1]
        assert kwargs["scale"] == 90.0
        assert "crsTransform" not in kwargs


class TestTheWindowSnapsOntoTheLattice:
    """A pinned grid has to start on a grid line, not at the AOI corner."""

    #: Pinned to 31.2 / 30.0, so an AOI starting at 31.2523 is mid-pixel.
    OFFSET = (31.2523, 29.95, 31.3, 30.0)

    def _grid(self, make_gee):
        """A pinned grid over an AOI that does not start on a grid line."""
        gee = _gee(
            make_gee,
            crs_transform=PINNED,
            lat_lim=[self.OFFSET[1], self.OFFSET[3]],
            lon_lim=[self.OFFSET[0], self.OFFSET[2]],
        )
        return gee._pinned_grid()

    def test_the_window_starts_on_a_grid_line(self, make_gee, fake_reader):
        """31.2523 snaps back to 31.252, a whole number of pixels from 31.2."""
        offset = (self._grid(make_gee).bbox[0] - PINNED[2]) / PINNED[0]
        assert offset == pytest.approx(round(offset), abs=1e-6)

    def test_it_snaps_outward_and_still_covers_the_aoi(self, make_gee, fake_reader):
        """Snapping inward would clip the window the caller asked for."""
        grid = self._grid(make_gee)
        assert grid.bbox[0] <= self.OFFSET[0]
        assert grid.bbox[2] >= self.OFFSET[2]

    def test_the_request_quotes_the_snapped_origin(self, make_gee, fake_reader):
        """Not the pinned one: that can lie far outside the AOI.

        Earth Engine places pixel (0, 0) at the transform's origin and returns
        `dimensions` pixels from there, so quoting 31.2 for a window starting at
        31.252 returned the right-sized raster over the wrong ground.
        """
        grid = self._grid(make_gee)
        assert grid.crs_transform[2] == grid.bbox[0]
        assert grid.crs_transform[5] == grid.bbox[3]
        assert grid.crs_transform[2] > PINNED[2]

    def test_two_different_aois_share_the_lattice(self, make_gee, fake_reader):
        """The guarantee the feature exists for, without a resample."""
        wide = _gee(make_gee, crs_transform=PINNED)._pinned_grid()
        narrow = self._grid(make_gee)
        assert wide.bbox[0] != narrow.bbox[0], "the two windows must differ"
        pixels_apart = (narrow.bbox[0] - wide.bbox[0]) / PINNED[0]
        assert pixels_apart == pytest.approx(round(pixels_apart), abs=1e-6)

    def test_shear_is_refused(self, make_gee, fake_reader):
        """A sheared lattice has no column to snap a window to."""
        with pytest.raises(ValueError, match="shear"):
            _gee(make_gee, crs_transform=(0.001, 0.5, 31.2, 0.0, -0.001, 30.0))


class TestTheReaderCannotHonourIt:
    """The EEDAI reader sizes its own metre grid and takes no transform."""

    def test_a_pinned_request_is_ineligible(self, make_gee, fake_reader):
        """Serving it would return pixels on a grid other than the pinned one."""
        gee = _gee(make_gee, crs_transform=PINNED)
        assert not gee._eedai_eligible(gee.catalog.get_dataset(ASSET))

    def test_the_same_request_unpinned_is_eligible(self, make_gee, fake_reader):
        """So the refusal is the transform, not something else about the read."""
        gee = _gee(make_gee)
        assert gee._eedai_eligible(gee.catalog.get_dataset(ASSET))

    def test_auto_routes_it_to_earth_engine(self, make_gee, fake_reader):
        """`auto` routes rather than fails, which is the whole contract."""
        gee = _gee(make_gee, crs_transform=PINNED)
        assert _export(gee).download_params["crs_transform"] == list(PINNED)

    def test_the_reader_is_never_asked(self, make_gee, fake_reader):
        """No scene discovery, no credential, for a request it cannot serve."""
        _export(_gee(make_gee, crs_transform=PINNED))
        assert fake_reader.calls == []

    def test_forced_eedai_names_the_pinned_grid(self, make_gee, fake_reader):
        """A CRS-shaped message for a transform-shaped problem misleads."""
        gee = _gee(make_gee, engine="eedai", crs_transform=PINNED)
        with pytest.raises(ValueError, match="crs_transform"):
            _export(gee)


class TestTheSizeCheckReadsThePinnedGrid:
    """A grid pinned finer than `scale` must not slip past the per-axis cap."""

    FINE = (1e-6, 0.0, 31.2, 0.0, -1e-6, 30.0)

    def test_an_oversized_pinned_grid_is_refused(self, make_gee, fake_reader):
        """100,000 px a side at 1e-6 degrees, over the 32768 cap."""
        gee = _gee(make_gee, engine="ee", crs_transform=self.FINE)
        with pytest.raises(ValueError, match="32,768|px"):
            _export(gee)

    def test_the_message_quotes_the_pinned_grid(self, make_gee, fake_reader):
        """Quoting a grid sized at `scale` would describe a different request."""
        gee = _gee(make_gee, engine="ee", crs_transform=self.FINE)
        with pytest.raises(ValueError, match=r"pinned by crs_transform=\[1e-06"):
            _export(gee)

    def test_the_message_counts_the_pinned_pixels(self, make_gee, fake_reader):
        """And the count is the pinned grid's, not the scale-sized one's."""
        gee = _gee(make_gee, engine="ee", crs_transform=self.FINE)
        with pytest.raises(ValueError, match="100001x100001 px"):
            _export(gee)

    def test_the_same_aoi_at_scale_is_not_refused(self, make_gee, fake_reader):
        """The scale-sized request fits, so only the pinning makes it too big."""
        assert _export(_gee(make_gee, engine="ee")).download_params["scale"] == 90.0


class TestWhatIsIgnoredSaysSo:
    """`nodata` and `high_volume` warn when the engine cannot honour them."""

    def test_nodata_warns_on_another_engine(self, make_gee, fake_reader, monkeypatch):
        """Only the pixel API's request body can declare it."""
        collected = _collect_warnings(monkeypatch)
        _gee(make_gee, engine="ee", nodata=-9999)
        assert any("nodata" in message for message in collected)

    def test_nodata_is_quiet_on_the_pixel_engine(
        self, make_gee, fake_reader, monkeypatch
    ):
        """There it is honoured, so there is nothing to report."""
        collected = _collect_warnings(monkeypatch)
        _gee(make_gee, engine="pixels", nodata=-9999)
        assert not [message for message in collected if "nodata" in message]

    def test_high_volume_warns_on_another_engine(
        self, make_gee, fake_reader, monkeypatch
    ):
        """The endpoint is documented for the pixel API, not for exports."""
        collected = _collect_warnings(monkeypatch)
        _gee(make_gee, engine="ee", high_volume=True)
        assert any("high_volume" in message for message in collected)

    def test_high_volume_is_quiet_on_the_pixel_engine(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Where it is used, it says nothing."""
        collected = _collect_warnings(monkeypatch)
        _gee(make_gee, engine="pixels", high_volume=True)
        assert not [message for message in collected if "high_volume" in message]
