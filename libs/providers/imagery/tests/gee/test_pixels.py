"""Tests for `PixelsEngine`, the engine that proves the Task 1.3 seam works."""

from __future__ import annotations

import pytest

from earthlens.gee.engines import ENGINES, Engine, resolve_engine
from earthlens.gee.grid import OutputGrid
from earthlens.gee.pixels import HIGH_VOLUME_ENDPOINT, PixelsEngine

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read.

    Built with a selectable engine: `engine="pixels"` is rejected at
    construction, so these tests drive `PixelsEngine` directly against a
    backend that holds the request.
    """
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    params.pop("engine", None)
    return make_gee(**params)


class TestTheSeamHeld:
    """The acceptance criterion for Task 1.3, asserted rather than asserted of."""

    def test_registering_is_enough_to_reach_the_backend(self):
        """One registry line, with no edit to `_use_eedai` or `_api`.

        Selectability is a separate question: an engine that cannot fetch is
        registered but kept out of the `engine=` vocabulary.
        """
        assert ENGINES["pixels"] is PixelsEngine
        with pytest.raises(ValueError, match="not selectable"):
            resolve_engine("pixels")

    def test_it_is_a_real_engine(self):
        """It satisfies the same ABC the two incumbents do."""
        assert issubclass(PixelsEngine, Engine)

    def test_it_is_registered_but_not_selectable(self, make_gee, fake_reader):
        """Registered to exercise the seam; not offered as a working choice."""
        from earthlens.gee.engines import selectable_engines

        assert "pixels" in ENGINES
        assert "pixels" not in selectable_engines()

    def test_the_constructor_rejects_it_with_a_reason(self, make_gee, fake_reader):
        """Rejected up front, not per-bucket after auth and the catalog read."""
        with pytest.raises(ValueError, match="not selectable"):
            make_gee(variables={ASSET: ["elevation"]}, scale=90.0, engine="pixels")


class TestItRefusesRatherThanMisroute:
    """`plan` must not return a servable verdict until the fetch is wired.

    `_api` sends every accepted plan to the EEDAI reader, which materialises
    *stored* pixels. A servable plan from here would therefore drop the
    `cloud_mask` or `filters` this engine exists to serve, silently.
    """

    def test_planning_refuses_with_a_reason(self, make_gee, fake_reader):
        """And the message names the engine to use instead."""
        gee = _gee(make_gee, engine="pixels")
        var_info = gee.catalog.get_dataset(ASSET)
        engine = PixelsEngine()
        with pytest.raises(NotImplementedError, match="cannot yet fetch"):
            engine.plan(gee, var_info, 1)

    def test_the_refusal_names_the_alternative(self, make_gee, fake_reader):
        """A refusal should say what to do, not just decline."""
        gee = _gee(make_gee, engine="pixels")
        var_info = gee.catalog.get_dataset(ASSET)
        engine = PixelsEngine()
        with pytest.raises(NotImplementedError, match="engine='auto'"):
            engine.plan(gee, var_info, 1)

    def test_a_masked_request_is_not_served_from_stored_pixels(
        self, make_gee, fake_reader
    ):
        """The specific data-correctness hole: compute must not be dropped."""
        gee = _gee(make_gee, engine="pixels", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        engine = PixelsEngine()
        with pytest.raises(NotImplementedError):
            engine.plan(gee, var_info, 1)


class TestCapability:
    """What the sizing half says it could serve, via `verdict`."""

    def test_it_would_serve_a_request_with_a_cloud_mask(self, make_gee, fake_reader):
        """`computePixels` evaluates the graph, so compute is not a decline."""
        gee = _gee(make_gee, engine="pixels", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        use_engine, plan = PixelsEngine().verdict(gee, var_info, 1)
        assert use_engine is True
        assert plan is not None
        assert plan.can_serve

    def test_it_would_serve_a_request_with_filters(self, make_gee, fake_reader):
        """The other half of the EEDAI eligibility gate it does not share."""
        gee = _gee(make_gee, engine="pixels", filters=[lambda c: c])
        var_info = gee.catalog.get_dataset(ASSET)
        assert PixelsEngine().verdict(gee, var_info, 1)[0] is True

    def test_the_eedai_reader_would_decline_the_same_request(
        self, make_gee, fake_reader
    ):
        """The contrast that makes this engine worth adding."""
        from earthlens.gee.engines import EedaiEngine

        gee = _gee(make_gee, engine="eedai", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        engine = EedaiEngine()
        with pytest.raises(ValueError, match="cannot serve"):
            engine.plan(gee, var_info, 1)

    def test_the_plan_names_this_engine(self, make_gee, fake_reader):
        """A plan says which engine produced it, which is why 1.6 added the field."""
        gee = _gee(make_gee, engine="pixels")
        var_info = gee.catalog.get_dataset(ASSET)
        _use, plan = PixelsEngine().verdict(gee, var_info, 1)
        assert plan.engine == "pixels"


class TestDeclines:
    """It declines only what the API genuinely cannot express."""

    def test_declines_a_dataset_with_no_resolvable_scale(self, make_gee, fake_reader):
        """Without a scale there is no grid to request."""
        gee = _gee(make_gee, engine="pixels", scale=None)
        var_info = gee.catalog.get_dataset(ASSET).model_copy(
            update={"spatial_resolution": None}
        )
        assert PixelsEngine().verdict(gee, var_info, 1) == (False, None)

    def test_declines_an_aoi_the_crs_cannot_represent(self, make_gee, fake_reader):
        """The same finiteness guard the grid applies, surfaced as a fallback."""
        ortho = "+proj=ortho +lat_0=0 +lon_0=0 +datum=WGS84 +units=m"
        gee = _gee(
            make_gee,
            engine="pixels",
            crs=ortho,
            lat_lim=[-10.0, 10.0],
            lon_lim=[150.0, 160.0],
        )
        var_info = gee.catalog.get_dataset(ASSET)
        assert PixelsEngine().verdict(gee, var_info, 1) == (False, None)

    def test_a_refusal_after_routing_is_fatal(self):
        """It is only reached by being asked for, so its refusal is the answer."""
        assert PixelsEngine().refusal_is_fatal() is True


class TestGridAndRequest:
    """It sizes from `OutputGrid` rather than deriving a grid of its own."""

    def test_the_grid_uses_the_metre_convention(self, make_gee, fake_reader):
        """`computePixels` takes explicit dimensions, so metres on the ground."""
        gee = _gee(make_gee, engine="pixels")
        grid = PixelsEngine().grid_for(gee, 90.0)
        assert isinstance(grid, OutputGrid)
        assert grid.convention == "metre"

    def test_the_request_carries_the_grid_dimensions(self, make_gee, fake_reader):
        """Width and height come from the shared grid, not a local calculation."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        body = engine.request(gee, "the-image", grid, ["elevation"])
        assert body["grid"]["dimensions"] == {"width": grid.cols, "height": grid.rows}
        assert body["grid"]["crsCode"] == grid.crs

    def test_the_request_passes_the_expression_and_format(self, make_gee, fake_reader):
        """`computePixels` evaluates `expression`, which is the computed image."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        body = engine.request(gee, "the-image", engine.grid_for(gee, 90.0), [])
        assert body["expression"] == "the-image"
        assert body["fileFormat"] == "GEO_TIFF"

    def test_bands_are_sent_when_requested(self, make_gee, fake_reader):
        """An empty band list means every band, so the key is omitted."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        assert engine.request(gee, "i", grid, ["B4"])["bandIds"] == ["B4"]
        assert "bandIds" not in engine.request(gee, "i", grid, [])

    def test_a_declared_nodata_reaches_the_request(self, make_gee, fake_reader):
        """The ExportConfig field added in 1.9, threaded through."""
        gee = _gee(make_gee, engine="pixels")
        # Set on the config directly: there is no public `nodata=` kwarg while
        # the fetch path that would honour it is unwired.
        object.__setattr__(
            gee,
            "_export_config",
            gee._export_config.model_copy(update={"nodata": -9999}),
        )
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        assert engine.request(gee, "i", grid, [])["grid"]["nodata"] == -9999

    def test_no_nodata_key_when_none_was_declared(self, make_gee, fake_reader):
        """Unset means today's behaviour, so the key is absent."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        assert "nodata" not in engine.request(gee, "i", grid, [])["grid"]

    def test_the_grid_carries_the_backends_crs_and_cutline(self, make_gee, fake_reader):
        """The engine reads the window from the backend rather than re-deriving it."""
        gee = _gee(make_gee, engine="pixels")
        grid = PixelsEngine().grid_for(gee, 90.0)
        bbox, cutline = gee._eedai_window()
        assert grid.crs == gee.crs
        assert grid.bbox == bbox
        assert grid.cutline is cutline

    def test_the_dataset_resolution_is_used_when_no_scale_was_given(
        self, make_gee, fake_reader
    ):
        """`scale=None` means the asset's native resolution, not no grid."""
        gee = _gee(make_gee, engine="pixels", scale=None)
        var_info = gee.catalog.get_dataset(ASSET)
        use_engine, plan = PixelsEngine().verdict(gee, var_info, 1)
        assert use_engine is True
        assert plan is not None
        native = PixelsEngine().grid_for(gee, float(var_info.spatial_resolution))
        assert plan.tiles == native.tiles(2048)

    def test_an_explicit_scale_overrides_the_dataset_resolution(
        self, make_gee, fake_reader
    ):
        """A coarser request must not silently read at the native resolution."""
        gee = _gee(make_gee, engine="pixels", scale=900.0)
        engine = PixelsEngine()
        coarse = engine.grid_for(gee, 900.0)
        fine = engine.grid_for(gee, 90.0)
        assert coarse.pixels < fine.pixels

    def test_the_plan_counts_tiles_at_the_shared_tile_size(self, make_gee, fake_reader):
        """A grid wider than one tile is reported as the tiles it cuts into."""
        gee = _gee(
            make_gee,
            engine="pixels",
            lat_lim=[29.0, 31.0],
            lon_lim=[31.0, 33.0],
            scale=30.0,
        )
        engine = PixelsEngine()
        var_info = gee.catalog.get_dataset(ASSET)
        expected = engine.grid_for(gee, 30.0).tiles(2048)
        _use, plan = engine.verdict(gee, var_info, 1)
        assert expected > 1, "the case has to be genuinely tiled to mean anything"
        assert plan.tiles == expected

    def test_the_tile_size_matches_the_eedai_streamers(self):
        """The docstring claims either engine cuts the same way, so pin it."""
        from earthlens.gee import backend as backend_module
        from earthlens.gee import pixels as pixels_module

        assert pixels_module._TILE_PIXELS == backend_module._EEDAI_TILE_PIXELS


class TestHighVolumeEndpoint:
    """Opted into per engine, because its trade-off is real."""

    def test_the_documented_host(self):
        """The host Earth Engine documents for parallel requests."""
        assert HIGH_VOLUME_ENDPOINT == "https://earthengine-highvolume.googleapis.com"

    def test_off_by_default(self):
        """Less caching and more EECU-time on complex queries, so not global."""
        assert PixelsEngine.endpoint(high_volume=False) is None

    def test_returned_when_asked_for(self):
        """Opting in selects the host for this engine only."""
        assert PixelsEngine.endpoint(high_volume=True) == HIGH_VOLUME_ENDPOINT

    def test_the_config_carries_the_flag(self):
        """`EngineConfig.high_volume`, defaulting off."""
        from earthlens.gee.configs import EngineConfig

        assert EngineConfig().high_volume is False
        assert EngineConfig(high_volume=True).high_volume is True
