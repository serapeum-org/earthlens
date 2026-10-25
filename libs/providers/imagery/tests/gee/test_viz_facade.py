"""Tests for the `Visualizer` delegation facade (Tasks H2 and M2).

digitalearth is faked through `sys.modules` so the whole seam is exercised
without the `viz` extra installed: these tests assert what earthlens *hands
over*, which is the only thing it is responsible for.
"""

from __future__ import annotations

import sys
import types

import pytest

from earthlens.gee.viz.facade import Visualizer


class _Recorder:
    """Records every call made on it, for asserting the delegation."""

    def __init__(self, name="scene"):
        self.name = name
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, verb):
        def call(*args, **kwargs):
            self.calls.append((verb, args, kwargs))
            return self

        return call

    def __getattr__(self, verb):
        return self._record(verb)

    def kwargs_of(self, verb):
        """Return the kwargs of the first call to `verb`."""
        return next(k for v, _, k in self.calls if v == verb)

    def args_of(self, verb):
        """Return the positional args of the first call to `verb`."""
        return next(a for v, a, _ in self.calls if v == verb)


class _FakeAnimation:
    """A stand-in for the matplotlib `FuncAnimation` digitalearth returns."""

    def __init__(self):
        self.saved_to = None

    def save(self, path, *args, **kwargs):
        self.saved_to = path


class _FakeMap(_Recorder):
    """A static Map whose `animate` returns a saveable animation."""

    def __init__(self):
        super().__init__("Map")
        self.animation = _FakeAnimation()

    def animate(self, stack, **kwargs):
        self.calls.append(("animate", (stack,), kwargs))
        return self.animation


@pytest.fixture
def fake_de(monkeypatch):
    """Install a fake `digitalearth` (plus its two tile tiers) in `sys.modules`."""
    module = types.ModuleType("digitalearth")
    quick = _Recorder("quickmap")
    static_map = _FakeMap()
    charts = {name: _Recorder(name) for name in ("line", "scatter", "histogram", "bar")}

    module.quickmap = lambda data, **kw: quick._record("quickmap")(data, **kw)
    module.Map = lambda *a, **kw: static_map
    for name, rec in charts.items():
        module.__dict__[name] = rec._record(name)

    web = types.ModuleType("digitalearth.web")
    web_map = _Recorder("WebMap")
    web.WebMap = lambda *a, **kw: web_map

    interactive = types.ModuleType("digitalearth.interactive")
    interactive_map = _Recorder("InteractiveMap")
    interactive.InteractiveMap = lambda *a, **kw: interactive_map

    monkeypatch.setitem(sys.modules, "digitalearth", module)
    monkeypatch.setitem(sys.modules, "digitalearth.web", web)
    monkeypatch.setitem(sys.modules, "digitalearth.interactive", interactive)
    return types.SimpleNamespace(
        module=module,
        quickmap=quick,
        static_map=static_map,
        charts=charts,
        web_map=web_map,
        interactive_map=interactive_map,
    )


class _FakeCube:
    """A pyramids `DatasetCollection` stand-in with a frame count."""

    def __init__(self, frames):
        self.time_length = frames


class _FakeResult:
    """A `DownloadResult` stand-in.

    `data` is a property on the real class, so it is an attribute here;
    `collection` is a **method** there (it stacks the written files via
    `from_files` and raises when none were written), so it is a method here —
    an attribute would hide a facade that forgot to call it.
    """

    def __init__(self, data="DATASET", frames=0, collection_error=None):
        self.data = data
        self._frames = frames
        self._collection_error = collection_error

    def collection(self, **kwargs):
        if self._collection_error is not None:
            raise self._collection_error
        return _FakeCube(self._frames)


class _FakeImage:
    """An `ee.Image` stand-in for the preview calls."""

    def getThumbURL(self, params):  # noqa: N802 - mirrors the ee SDK's spelling
        self.thumb_params = params
        return "https://example.invalid/thumb.png"

    def getMapId(self, params):  # noqa: N802 - mirrors the ee SDK's spelling
        self.map_params = params
        return {"mapid": "m", "urlFormat": "https://x/{z}/{x}/{y}"}


def _viz(result=None, bands=("elevation",)):
    """Build a visualizer over a real catalog row."""
    return Visualizer(result, "USGS/SRTMGL1_003", bands)


class TestConstruction:
    """Hints are resolved up front, from the catalog."""

    def test_hints_are_resolved_at_construction(self):
        """The catalog is consulted once, not per render."""
        assert _viz().hints.bands == ("elevation",)

    def test_repr_names_the_asset_and_the_hint_source(self):
        """A visualizer says what it will draw and where the style came from."""
        viz = _viz()
        assert "USGS/SRTMGL1_003" in repr(viz)
        assert viz.hints.source in repr(viz)

    def test_an_unknown_band_fails_at_construction_not_at_render(self):
        """A mistake surfaces before any renderer is involved."""
        with pytest.raises(ValueError, match="is not a band of"):
            Visualizer(None, "USGS/SRTMGL1_003", ("nope",))


def _rgb_viz(result):
    """A visualizer for a real three-band dataset (so `is_rgb` is true)."""
    return Visualizer(result, "COPERNICUS/S2_SR_HARMONIZED", ("B4", "B3", "B2"))


class TestStillSingleBand:
    """A single-band `still` delegates to `quickmap` with a vmin/vmax stretch."""

    def test_the_data_and_backend_reach_quickmap(self, fake_de):
        """earthlens hands over the object and the chosen tier."""
        _viz(_FakeResult()).still(backend="interactive")
        args = fake_de.quickmap.args_of("quickmap")
        kwargs = fake_de.quickmap.kwargs_of("quickmap")
        assert args[0] == "DATASET"
        assert kwargs["backend"] == "interactive"

    def test_the_stretch_is_passed_as_vmin_vmax_not_limits(self, fake_de):
        """The single-band path takes a scalar clim, which it calls vmin/vmax."""
        viz = _viz(_FakeResult())
        viz.still()
        kwargs = fake_de.quickmap.kwargs_of("quickmap")
        (lo, hi) = viz.hints.limits[0]
        assert (kwargs["vmin"], kwargs["vmax"]) == (lo, hi)
        assert "limits" not in kwargs

    def test_an_explicit_kwarg_overrides_the_hint(self, fake_de):
        """The caller keeps the last word over the catalog."""
        _viz(_FakeResult()).still(cmap="magma")
        assert fake_de.quickmap.kwargs_of("quickmap")["cmap"] == "magma"

    def test_the_default_backend_is_matplotlib(self, fake_de):
        """digitalearth's own default tier, spelled its way."""
        _viz(_FakeResult()).still()
        assert fake_de.quickmap.kwargs_of("quickmap")["backend"] == "matplotlib"

    @pytest.mark.parametrize("backend", ["matplotlib", "interactive", "web", "3d"])
    def test_every_digitalearth_backend_is_accepted(self, fake_de, backend):
        """The four tier names are the vocabulary, as quickmap spells them."""
        _viz(_FakeResult()).still(backend=backend)
        assert fake_de.quickmap.kwargs_of("quickmap")["backend"] == backend

    def test_an_unknown_backend_is_refused_before_importing_digitalearth(self):
        """The check runs with no digitalearth installed at all."""
        with pytest.raises(ValueError, match="is not a digitalearth backend"):
            _viz(_FakeResult()).still(backend="static")

    def test_a_missing_result_is_refused(self):
        """There is nothing to render without a download."""
        with pytest.raises(ValueError, match="without a result"):
            _viz().still()

    def test_a_paths_only_result_names_materialize(self, fake_de):
        """The remedy is the flag the caller did not pass."""
        with pytest.raises(ValueError, match="materialize=True"):
            _viz(_FakeResult(data=None)).still()


class TestStillRgb:
    """A three-band `still` routes to the tier's rgb_composite, not quickmap."""

    def test_matplotlib_routes_to_the_static_rgb_composite(self, fake_de):
        """quickmap has no RGB renderer, so a Map draws it."""
        _rgb_viz(_FakeResult()).still(backend="matplotlib")
        assert fake_de.static_map.args_of("rgb_composite")[0] == "DATASET"
        assert fake_de.quickmap.calls == []

    def test_the_per_band_limits_are_passed_not_band_names(self, fake_de):
        """rgb_composite reads band order, so `limits` go through and names do not."""
        _rgb_viz(_FakeResult()).still(backend="matplotlib")
        kwargs = fake_de.static_map.kwargs_of("rgb_composite")
        assert kwargs["limits"] == [(0.0, 3000.0), (0.0, 3000.0), (0.0, 3000.0)]
        assert "bands" not in kwargs

    def test_the_web_tier_also_uses_rgb_composite(self, fake_de):
        """The web tier spells it the same way."""
        _rgb_viz(_FakeResult()).still(backend="web")
        assert "limits" in fake_de.web_map.kwargs_of("rgb_composite")

    def test_the_interactive_tier_spells_the_verb_rgb(self, fake_de):
        """The interactive tier's composite verb is `rgb`, not `rgb_composite`."""
        _rgb_viz(_FakeResult()).still(backend="interactive")
        assert "limits" in fake_de.interactive_map.kwargs_of("rgb")

    def test_the_3d_tier_has_no_composite_and_is_refused(self, fake_de):
        """3-D has no RGB composite renderer, so a composite there is refused."""
        with pytest.raises(ValueError, match="no RGB composite renderer"):
            _rgb_viz(_FakeResult()).still(backend="3d")


class TestTimelapse:
    """`timelapse` delegates to the static tier's `animate`."""

    def test_the_stacked_collection_is_handed_over_once(self, fake_de, tmp_path):
        """Collect-then-emit-once: the stacked cube goes to animate in one call."""
        _viz(_FakeResult(frames=3)).timelapse(tmp_path / "out.gif")
        (cube,) = fake_de.static_map.args_of("animate")
        assert isinstance(cube, _FakeCube)

    def test_the_animation_is_saved_to_the_given_path(self, fake_de, tmp_path):
        """earthlens writes the file digitalearth's animation produces."""
        out = tmp_path / "out.gif"
        _viz(_FakeResult(frames=3)).timelapse(out)
        assert fake_de.static_map.animation.saved_to == str(out)

    def test_a_single_band_request_animates_as_imshow(self, fake_de, tmp_path):
        """digitalearth's own default kind for a one-band stack."""
        _viz(_FakeResult(frames=3)).timelapse(tmp_path / "o.gif")
        assert fake_de.static_map.kwargs_of("animate")["kind"] == "imshow"

    def test_a_three_band_request_animates_as_an_rgb_composite(self, fake_de, tmp_path):
        """A composite keeps being a composite across the time axis."""
        viz = Visualizer(
            _FakeResult(frames=3),
            "COPERNICUS/S2_SR_HARMONIZED",
            ("B4", "B3", "B2"),
        )
        viz.timelapse(tmp_path / "o.gif")
        assert fake_de.static_map.kwargs_of("animate")["kind"] == "rgb_composite"

    def test_a_single_frame_result_is_refused(self, fake_de, tmp_path):
        """A single raster is not a time series, and says so."""
        with pytest.raises(ValueError, match="not a time series"):
            _viz(_FakeResult(frames=1)).timelapse(tmp_path / "o.gif")

    def test_a_batch_export_result_propagates_the_collection_error(
        self, fake_de, tmp_path
    ):
        """`collection()` raises for a batch export, and that reaches the caller."""
        err = ValueError("no bucket wrote a file")
        result = _FakeResult(collection_error=err)
        with pytest.raises(ValueError, match="no bucket wrote a file"):
            _viz(result).timelapse(tmp_path / "o.gif")

    def test_a_missing_result_is_refused(self, tmp_path):
        """Nothing to animate without a download."""
        with pytest.raises(ValueError, match="without a result"):
            _viz().timelapse(tmp_path / "o.gif")


class TestChart:
    """`chart` delegates to digitalearth's own chart verbs."""

    @pytest.mark.parametrize("kind", ["line", "scatter", "histogram", "bar"])
    def test_each_chart_verb_is_reachable(self, fake_de, kind):
        """earthlens adds no charting of its own; it forwards the verb."""
        _viz(_FakeResult()).chart(kind, "SERIES")
        assert fake_de.charts[kind].args_of(kind)[0] == "SERIES"

    def test_an_unknown_chart_kind_is_refused_with_the_choices(self):
        """A typo names what digitalearth does offer."""
        with pytest.raises(ValueError, match="is not a digitalearth chart"):
            _viz(_FakeResult()).chart("piechart", "SERIES")


class TestServerPreviews:
    """The two Earth-Engine-side previews, which need no digitalearth."""

    def test_thumbnail_returns_the_url_and_applies_the_hints(self):
        """A still preview is stretched by the catalog's hints."""
        image = _FakeImage()
        viz = _viz()
        url = viz.thumbnail(image, dimensions=256)
        assert url.endswith(".png")
        assert image.thumb_params["dimensions"] == 256
        assert image.thumb_params["min"] == [viz.hints.limits[0][0]]

    def test_tiles_returns_a_template(self):
        """The tile endpoint resolves to an XYZ template."""
        assert "{z}/{x}/{y}" in _viz().tiles(_FakeImage()).url


class TestTileMap:
    """M2: the EE tile URL reaches digitalearth's `tiles(url=...)`."""

    def test_the_web_tier_receives_the_earth_engine_url(self, fake_de):
        """The whole seam: earthlens resolves, digitalearth draws."""
        _viz().tile_map(_FakeImage(), backend="web", name="DEM")
        kwargs = fake_de.web_map.kwargs_of("tiles")
        assert "{z}/{x}/{y}" in kwargs["url"]
        assert kwargs["name"] == "DEM"

    def test_the_attribution_is_carried_onto_the_layer(self, fake_de):
        """Earth Engine's terms require credit on a published map."""
        _viz().tile_map(_FakeImage())
        assert fake_de.web_map.kwargs_of("tiles")["attribution"] == (
            "Google Earth Engine"
        )

    def test_the_interactive_tier_is_also_supported(self, fake_de):
        """Both slippy-map tiers accept an arbitrary XYZ template."""
        _viz().tile_map(_FakeImage(), backend="interactive")
        assert "url" in fake_de.interactive_map.kwargs_of("tiles")

    def test_extra_kwargs_reach_the_tile_layer(self, fake_de):
        """digitalearth's own tile options stay available."""
        _viz().tile_map(_FakeImage(), opacity=0.5)
        assert fake_de.web_map.kwargs_of("tiles")["opacity"] == 0.5

    @pytest.mark.parametrize("backend", ["matplotlib", "3d"])
    def test_a_non_tile_backend_is_refused(self, backend):
        """The static tier draws no slippy map and 3-D spells it differently."""
        with pytest.raises(ValueError, match="is not a digitalearth backend"):
            _viz().tile_map(_FakeImage(), backend=backend)

    def test_no_local_data_is_needed(self, fake_de):
        """A tile map downloads nothing, so it works with no bound result."""
        _viz(result=None).tile_map(_FakeImage())
        assert fake_de.web_map.kwargs_of("tiles")


class TestTheExtraIsNotRequired:
    """Importing the facade must not need digitalearth."""

    def test_a_missing_digitalearth_names_the_extra(self, monkeypatch):
        """The error tells the caller what to install, not what failed."""
        monkeypatch.setitem(sys.modules, "digitalearth", None)
        with pytest.raises(ImportError, match=r"earthlens\[viz\]"):
            _viz(_FakeResult()).still()
