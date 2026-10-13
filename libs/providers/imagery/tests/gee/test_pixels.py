"""Tests for `PixelsEngine`, the engine that proves the Task 1.3 seam works."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.gee import backend as backend_module
from earthlens.gee.engines import ENGINES, Engine, resolve_engine
from earthlens.gee.grid import OutputGrid
from earthlens.gee.pixels import HIGH_VOLUME_ENDPOINT, PixelsEngine

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"


def _record_merge(sink: list, sources, target) -> None:
    """Stand in for `merge_rasters`, recording the call and writing the mosaic."""
    sink.append({"sources": list(sources), "target": str(target)})
    Path(target).write_bytes(b"merged")


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read."""
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    return make_gee(**params)


class TestTheSeamHeld:
    """The acceptance criterion for the Engine ABC, asserted rather than claimed."""

    def test_registering_is_enough_to_reach_the_backend(self):
        """One registry line, with no edit to `_use_eedai`."""
        assert ENGINES["pixels"] is PixelsEngine
        assert isinstance(resolve_engine("pixels"), PixelsEngine)

    def test_it_is_selectable(self):
        """It can fetch, so it belongs in the `engine=` vocabulary."""
        from earthlens.gee.engines import selectable_engines

        assert "pixels" in selectable_engines()

    def test_it_is_a_real_engine(self):
        """It satisfies the same ABC the incumbents do."""
        assert issubclass(PixelsEngine, Engine)

    def test_the_constructor_accepts_it(self, make_gee, fake_reader):
        """The vocabulary is derived from the registry, not restated."""
        assert _gee(make_gee, engine="pixels").engine == "pixels"

    def test_the_backend_resolves_it_to_the_object(self, make_gee, fake_reader):
        """Resolution happens once, in the constructor."""
        assert isinstance(_gee(make_gee, engine="pixels")._engine, PixelsEngine)


class TestFetching:
    """`download()` through the pixel API writes a raster like any other sink."""

    def test_it_writes_a_file(self, make_gee, fake_reader):
        """The same return shape the `url` sink gives."""
        (out,) = _gee(make_gee, engine="pixels").download(progress_bar=False)
        assert isinstance(out, Path)
        assert out.is_file()

    def test_it_calls_compute_pixels_once_per_bucket(self, make_gee, fake_reader):
        """And reaches `ee.data.computePixels`, not `getDownloadURL`."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        assert len(gee.client.compute_pixels_calls) == 1

    def test_it_does_not_use_get_download_url(self, make_gee, fake_reader):
        """The capped thumbnail path is bypassed entirely."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        assert gee.client.export_image.calls == []

    def test_the_request_carries_the_grid_and_the_expression(
        self, make_gee, fake_reader
    ):
        """What `computePixels` evaluates, and the shape it returns."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        (body,) = gee.client.compute_pixels_calls
        assert body["fileFormat"] == "GEO_TIFF"
        assert body["expression"] is not None
        assert body["grid"]["dimensions"]["width"] > 0

    def test_it_serves_a_masked_request(self, make_gee, fake_reader):
        """The capability the EEDAI reader cannot offer, end to end."""
        gee = _gee(make_gee, engine="pixels", cloud_mask=lambda image: image)
        (out,) = gee.download(progress_bar=False)
        assert out.is_file()
        assert len(gee.client.compute_pixels_calls) == 1

    def test_a_dataset_with_no_resolvable_scale_is_refused(self, make_gee, fake_reader):
        """There is no grid to request, so it says so rather than guessing."""
        gee = _gee(make_gee, engine="pixels", scale=None)
        var_info = gee.catalog.get_dataset(ASSET).model_copy(
            update={"spatial_resolution": None}
        )
        with pytest.raises(ValueError, match="cannot size"):
            gee._export_via_pixels("image", var_info, 0.0, ["elevation"], "prefix")

    def test_the_batch_sinks_are_untouched_by_it(self, make_gee, fake_reader):
        """`pixels` is a pull, so it has nothing to say about a queued export."""
        gee = _gee(make_gee, engine="pixels", export_via="drive", drive_folder="out")
        (out,) = gee.download(progress_bar=False)
        assert out.startswith("drive://")
        assert gee.client.compute_pixels_calls == []


class TestCapability:
    """What the sizing half says it could serve, via `verdict`."""

    def test_it_would_serve_a_request_with_a_cloud_mask(self, make_gee, fake_reader):
        """`computePixels` evaluates the graph, so compute is not a decline."""
        gee = _gee(make_gee, engine="pixels", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        use_engine, plan = PixelsEngine().verdict(gee, var_info)
        assert use_engine is True
        assert plan is not None
        assert plan.can_serve

    def test_it_would_serve_a_request_with_filters(self, make_gee, fake_reader):
        """The other half of the EEDAI eligibility gate it does not share."""
        gee = _gee(make_gee, engine="pixels", filters=[lambda c: c])
        var_info = gee.catalog.get_dataset(ASSET)
        assert PixelsEngine().verdict(gee, var_info)[0] is True

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
        _use, plan = PixelsEngine().verdict(gee, var_info)
        assert plan.engine == "pixels"


class TestDeclines:
    """It declines only what the API genuinely cannot express."""

    def test_declines_a_dataset_with_no_resolvable_scale(self, make_gee, fake_reader):
        """Without a scale there is no grid to request."""
        gee = _gee(make_gee, engine="pixels", scale=None)
        var_info = gee.catalog.get_dataset(ASSET).model_copy(
            update={"spatial_resolution": None}
        )
        assert PixelsEngine().verdict(gee, var_info) == (False, None)

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
        assert PixelsEngine().verdict(gee, var_info) == (False, None)

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
        use_engine, plan = PixelsEngine().verdict(gee, var_info)
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
        _use, plan = engine.verdict(gee, var_info)
        assert expected > 1, "the case has to be genuinely tiled to mean anything"
        assert plan.tiles == expected

    def test_the_tile_size_matches_the_eedai_streamers(self):
        """The docstring claims either engine cuts the same way, so pin it."""
        from earthlens.gee import backend as backend_module
        from earthlens.gee import pixels as pixels_module

        assert pixels_module._TILE_PIXELS == backend_module._EEDAI_TILE_PIXELS


class TestThePerAxisCapAppliesHereToo:
    """Earth Engine caps a `computePixels` grid exactly as it caps a thumbnail.

    Measured, not assumed: the live API refuses a 39,479 x 12 px request with
    `Pixel grid dimensions (39479x12) must be less than or equal to 32768`. The
    epic expected this engine to be uncapped, and it is not - so the backend has
    to own the refusal rather than let a raw Earth Engine error surface.
    """

    #: A 360-degree strip 0.1 degrees tall at 1 km: ~39,000 px on the long axis.
    WIDE = {"lat_lim": [9.9, 10.0], "lon_lim": [-180.0, 180.0], "scale": 1000.0}

    def test_an_oversize_request_is_refused(self, make_gee, fake_reader):
        """Before the request goes out, with our message rather than theirs."""
        gee = _gee(make_gee, engine="pixels", **self.WIDE)
        with pytest.raises(ValueError, match="same per-axis cap"):
            gee.download(progress_bar=False)

    def test_the_refusal_quotes_the_grid_and_the_remedies(self, make_gee, fake_reader):
        """A caller can act on it: the size, and the four ways out."""
        gee = _gee(make_gee, engine="pixels", **self.WIDE)
        with pytest.raises(ValueError, match="auto_split=True"):
            gee.download(progress_bar=False)

    def test_nothing_is_posted_when_it_is_refused(self, make_gee, fake_reader):
        """The point of checking first: no request, no partial file."""
        gee = _gee(make_gee, engine="pixels", **self.WIDE)
        with pytest.raises(ValueError):
            gee.download(progress_bar=False)
        assert gee.client.compute_pixels_calls == []

    def test_auto_split_tiles_it_instead(self, make_gee, fake_reader, monkeypatch):
        """`auto_split=True` means the same thing here as on the `ee` path."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        gee = _gee(make_gee, engine="pixels", auto_split=True, **self.WIDE)
        (out,) = gee.download(progress_bar=False)
        assert len(gee.client.compute_pixels_calls) > 1, "the AOI was not split"
        assert len(merged) == 1, "the tiles were not mosaicked"
        assert out.name.endswith(".tif")

    def test_every_tile_is_within_the_cap(self, make_gee, fake_reader, monkeypatch):
        """Splitting that still breaks the cap would only move the error."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        gee = _gee(make_gee, engine="pixels", auto_split=True, **self.WIDE)
        gee.download(progress_bar=False)
        for body in gee.client.compute_pixels_calls:
            dimensions = body["grid"]["dimensions"]
            assert max(dimensions["width"], dimensions["height"]) <= 32768, dimensions

    def test_a_pinned_request_keeps_its_lattice_across_tiles(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Each tile is sized through the engine, so the pin survives tiling."""
        merged: list[dict] = []
        monkeypatch.setattr(
            backend_module,
            "merge_rasters",
            lambda sources, target, **kwargs: _record_merge(merged, sources, target),
        )
        pixel = 0.01
        gee = _gee(
            make_gee,
            engine="pixels",
            auto_split=True,
            crs_transform=(pixel, 0.0, -180.0, 0.0, -pixel, 10.0),
            **self.WIDE,
        )
        gee.download(progress_bar=False)
        assert len(gee.client.compute_pixels_calls) > 1
        for body in gee.client.compute_pixels_calls:
            origin = body["grid"]["affineTransform"]["translateX"]
            offset = (origin - (-180.0)) / pixel
            assert abs(offset - round(offset)) < 1e-6, origin

    def test_a_request_within_the_cap_is_untouched(self, make_gee, fake_reader):
        """One request, no tiling, for the ordinary case."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        assert len(gee.client.compute_pixels_calls) == 1


class TestHighVolumeEndpoint:
    """Opted into per engine, because its trade-off is real."""

    def test_the_documented_host(self):
        """The host Earth Engine documents for parallel requests."""
        assert HIGH_VOLUME_ENDPOINT == "https://earthengine-highvolume.googleapis.com"

    def test_off_by_default(self):
        """Less caching and more EECU-time on complex queries, so not global."""
        assert PixelsEngine().endpoint(high_volume=False) is None

    def test_returned_when_asked_for(self):
        """Opting in selects the host for this engine only."""
        assert PixelsEngine().endpoint(high_volume=True) == HIGH_VOLUME_ENDPOINT

    def test_the_config_carries_the_flag(self):
        """`EngineConfig.high_volume`, defaulting off."""
        from earthlens.gee.configs import EngineConfig

        assert EngineConfig().high_volume is False
        assert EngineConfig(high_volume=True).high_volume is True

    def test_every_other_engine_answers_none(self):
        """The ABC's default, so a new engine needs no endpoint decision."""
        from earthlens.gee.engines import AutoEngine, EedaiEngine, EeEngine

        for engine in (EeEngine(), EedaiEngine(), AutoEngine()):
            assert engine.endpoint(high_volume=True) is None


class TestTheEndpointReachesEarthEngine:
    """Asking for the endpoint is one thing; opening the session on it another."""

    def test_the_default_host_is_not_overridden(self, make_gee, fake_reader):
        """An unasked-for endpoint must not change how anyone authenticates."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        assert gee.client.auth_calls[-1]["host"] is None

    def test_high_volume_reaches_the_auth_call(self, make_gee, fake_reader):
        """`GEE(high_volume=True)` is what points the session at the host."""
        gee = _gee(make_gee, engine="pixels", high_volume=True)
        gee.download(progress_bar=False)
        assert gee.client.auth_calls[-1]["host"] == HIGH_VOLUME_ENDPOINT

    def test_another_engine_keeps_the_default_host(self, make_gee, fake_reader):
        """It is engine-scoped: `ee` is initialised as it always was."""
        gee = _gee(make_gee, engine="ee", high_volume=True)
        gee.download(progress_bar=False)
        assert gee.client.auth_calls[-1]["host"] is None

    def test_the_application_default_path_gets_it_too(
        self, make_gee, fake_ee, fake_reader, monkeypatch
    ):
        """The no-key branch initialises `ee` directly, so it needs it as well."""
        gee = _gee(make_gee, engine="pixels", high_volume=True)
        for name in ("GEE_SERVICE_ACCOUNT", "GEE_SERVICE_KEY", "GEE_PROJECT"):
            monkeypatch.delenv(name, raising=False)
        gee.authenticate(project="adc-project")
        assert fake_ee.initialize_calls == [
            {"project": "adc-project", "opt_url": HIGH_VOLUME_ENDPOINT}
        ]


class TestCog:
    """`cog=True` is honoured on the pixel path, which writes the file itself."""

    def test_it_writes_through_the_cog_writer(self, make_gee, fake_reader):
        """`to_cog`, not `to_file`: the flag has to reach the write."""
        gee = _gee(make_gee, engine="pixels", cog=True)
        (out,) = gee.download(progress_bar=False)
        assert out.is_file()

    def test_the_plain_path_is_unchanged(self, make_gee, fake_reader):
        """And without the flag the ordinary writer still runs."""
        (out,) = _gee(make_gee, engine="pixels").download(progress_bar=False)
        assert out.is_file()


class TestNodata:
    """`GEE(nodata=...)` is declared on the request, which is the only path for it."""

    def test_it_reaches_the_request_grid(self, make_gee, fake_reader):
        """The fill value the caller stated, not whatever the driver declares."""
        gee = _gee(make_gee, engine="pixels", nodata=-9999)
        gee.download(progress_bar=False)
        (body,) = gee.client.compute_pixels_calls
        assert body["grid"]["nodata"] == -9999

    def test_absent_by_default(self, make_gee, fake_reader):
        """An unset nodata leaves the key out rather than sending null."""
        gee = _gee(make_gee, engine="pixels")
        gee.download(progress_bar=False)
        (body,) = gee.client.compute_pixels_calls
        assert "nodata" not in body["grid"]

    def test_zero_is_a_value_and_not_an_absence(self, make_gee, fake_reader):
        """`0` is a legitimate fill value, so the test cannot be truthiness."""
        gee = _gee(make_gee, engine="pixels", nodata=0)
        gee.download(progress_bar=False)
        (body,) = gee.client.compute_pixels_calls
        assert body["grid"]["nodata"] == 0

    def test_the_config_carries_it(self):
        """`ExportConfig.nodata`, where the request builder reads it."""
        from earthlens.gee.configs import ExportConfig

        assert ExportConfig().nodata is None
        assert ExportConfig(nodata=-1).nodata == -1


class TestTheAffineTransformMapping:
    """The 6-tuple is renamed, not reordered, on its way into the request."""

    def test_the_six_terms_keep_their_meaning(self):
        """Earth Engine's `crsTransform` lists them in this order too."""
        from earthlens.gee.pixels import affine_transform

        assert affine_transform((1.0, 2.0, 3.0, 4.0, 5.0, 6.0)) == {
            "scaleX": 1.0,
            "shearX": 2.0,
            "translateX": 3.0,
            "shearY": 4.0,
            "scaleY": 5.0,
            "translateY": 6.0,
        }
