"""Tests for `PixelsEngine`, the engine that proves the Task 1.3 seam works."""

from __future__ import annotations

import pytest

from earthlens.gee.engines import ENGINES, Engine, resolve_engine
from earthlens.gee.grid import OutputGrid
from earthlens.gee.pixels import HIGH_VOLUME_ENDPOINT, PixelsEngine

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read."""
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    return make_gee(**params)


class TestTheSeamHeld:
    """The acceptance criterion for Task 1.3, asserted rather than asserted of."""

    def test_registering_is_enough_to_make_it_selectable(self):
        """No edit to `_use_eedai` or `_api` was needed — only this entry."""
        assert ENGINES["pixels"] is PixelsEngine
        assert isinstance(resolve_engine("pixels"), PixelsEngine)

    def test_it_is_a_real_engine(self):
        """It satisfies the same ABC the two incumbents do."""
        assert issubclass(PixelsEngine, Engine)

    def test_the_constructor_accepts_it(self, make_gee, fake_reader):
        """The `engine=` vocabulary is derived from the registry, not restated."""
        assert _gee(make_gee, engine="pixels").engine == "pixels"

    def test_the_backend_resolves_it_to_the_object(self, make_gee, fake_reader):
        """Resolution happens once, in the constructor."""
        assert isinstance(_gee(make_gee, engine="pixels")._engine, PixelsEngine)


class TestCapability:
    """What this engine can do that the EEDAI reader cannot."""

    def test_it_serves_a_request_with_a_cloud_mask(self, make_gee, fake_reader):
        """`computePixels` evaluates the graph, so compute is not a decline."""
        gee = _gee(make_gee, engine="pixels", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        use_engine, plan = PixelsEngine().plan(gee, var_info, 1)
        assert use_engine is True
        assert plan is not None and plan.can_serve

    def test_it_serves_a_request_with_filters(self, make_gee, fake_reader):
        """The other half of the EEDAI eligibility gate it does not share."""
        gee = _gee(make_gee, engine="pixels", filters=[lambda c: c])
        var_info = gee.catalog.get_dataset(ASSET)
        assert PixelsEngine().plan(gee, var_info, 1)[0] is True

    def test_the_eedai_reader_would_decline_the_same_request(
        self, make_gee, fake_reader
    ):
        """The contrast that makes this engine worth adding."""
        from earthlens.gee.engines import EedaiEngine

        gee = _gee(make_gee, engine="eedai", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        with pytest.raises(ValueError, match="cannot serve"):
            EedaiEngine().plan(gee, var_info, 1)

    def test_the_plan_names_this_engine(self, make_gee, fake_reader):
        """A plan says which engine produced it, which is why 1.6 added the field."""
        gee = _gee(make_gee, engine="pixels")
        var_info = gee.catalog.get_dataset(ASSET)
        _use, plan = PixelsEngine().plan(gee, var_info, 1)
        assert plan.engine == "pixels"


class TestDeclines:
    """It declines only what the API genuinely cannot express."""

    def test_declines_a_dataset_with_no_resolvable_scale(self, make_gee, fake_reader):
        """Without a scale there is no grid to request."""
        gee = _gee(make_gee, engine="pixels", scale=None)
        var_info = gee.catalog.get_dataset(ASSET).model_copy(
            update={"spatial_resolution": None}
        )
        assert PixelsEngine().plan(gee, var_info, 1) == (False, None)

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
        assert PixelsEngine().plan(gee, var_info, 1) == (False, None)

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
        gee = _gee(make_gee, engine="pixels", nodata=-9999)
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        assert engine.request(gee, "i", grid, [])["grid"]["nodata"] == -9999

    def test_no_nodata_key_when_none_was_declared(self, make_gee, fake_reader):
        """Unset means today's behaviour, so the key is absent."""
        gee = _gee(make_gee, engine="pixels")
        engine = PixelsEngine()
        grid = engine.grid_for(gee, 90.0)
        assert "nodata" not in engine.request(gee, "i", grid, [])["grid"]


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
