"""Tests for the `Engine` strategy objects and the routing they now own."""

from __future__ import annotations

import datetime as dt
import sys
from types import SimpleNamespace

import pytest

from earthlens.gee.engines import (
    ENGINES,
    AutoEngine,
    EedaiEngine,
    EeEngine,
    Engine,
    reader_refusal_types,
    resolve_engine,
)

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

ASSET = "USGS/SRTMGL1_003"
START = dt.datetime(2020, 6, 1)
END = dt.datetime(2020, 7, 1)


class _FakeReaderError(Exception):
    """Stands in for `pyramids_eo.errors.ReaderError`, which may not be installed."""


def _raise_missing_extra():
    """Fail the guarded import the way an uninstalled `[eedai]` extra does."""
    raise ImportError("the pyramids-eo EEDAI reader is required for this path")


class _UnwiredEngine(Engine):
    """An engine that registers to prove the seam but cannot fetch yet."""

    name = "unwired"
    implemented = False

    def plan(self, backend, var_info, band_count, bucket_start=None, bucket_end=None):
        """Never serves; it exists to be refused by `resolve_engine`."""
        return False, None


def _gee(make_gee, **overrides):
    """A GEE over one small SRTM read."""
    params = {"variables": {ASSET: ["elevation"]}, "scale": 90.0}
    params.update(overrides)
    return make_gee(**params)


class TestRegistry:
    """What `engine=` resolves to."""

    def test_the_shipped_engines_are_registered(self):
        """The vocabulary the constructor validates against.

        `pixels` registers itself on import (Task 1.13), which is the mechanism
        that makes a new engine selectable without a backend edit.
        """
        from earthlens.gee import pixels  # noqa: F401 - registers PixelsEngine

        assert set(ENGINES) == {"auto", "ee", "eedai", "pixels"}

    @pytest.mark.parametrize(
        ("name", "cls"),
        [("auto", AutoEngine), ("ee", EeEngine), ("eedai", EedaiEngine)],
    )
    def test_each_name_resolves_to_its_class(self, name, cls):
        """A string becomes the matching engine object."""
        assert isinstance(resolve_engine(name), cls)
        assert cls.name == name

    def test_every_engine_implements_the_abc(self):
        """Each registered class is a real `Engine`."""
        for cls in ENGINES.values():
            assert issubclass(cls, Engine)

    def test_an_instance_passes_through(self):
        """An already-built engine is returned unchanged."""
        engine = EeEngine()
        assert resolve_engine(engine) is engine

    def test_an_unknown_name_lists_the_alternatives(self):
        """The error names what is available."""
        with pytest.raises(ValueError, match=r"engine must be one of"):
            resolve_engine("telepathy")

    def test_a_non_string_is_a_type_error(self):
        """A stray object fails loudly rather than being coerced."""
        with pytest.raises(TypeError, match="name or an Engine"):
            resolve_engine(3)

    def test_repr_names_the_class(self):
        """Engines appear in log lines, so they must read cleanly."""
        assert repr(EedaiEngine()) == "EedaiEngine()"

    def test_the_base_class_cannot_be_instantiated(self):
        """`plan` is abstract, so a bare `Engine` cannot route anything."""
        with pytest.raises(TypeError):
            Engine()

    def test_each_resolution_builds_a_fresh_instance(self):
        """The registry holds classes, so no state leaks between backends."""
        assert resolve_engine("auto") is not resolve_engine("auto")


class TestTheSelectableVocabulary:
    """Registering an engine is not the same as offering it as a choice.

    Every shipped engine can fetch today, so without a stand-in nothing
    exercises the refusal - which is how it stopped being covered when the
    pixel engine's transport landed.
    """

    def test_a_registered_but_unwired_engine_is_refused(self, monkeypatch):
        """Named, so the message can say "not yet" rather than "no such engine"."""
        monkeypatch.setitem(ENGINES, _UnwiredEngine.name, _UnwiredEngine)
        with pytest.raises(ValueError, match="cannot fetch pixels yet"):
            resolve_engine(_UnwiredEngine.name)

    def test_it_is_left_out_of_the_vocabulary(self, monkeypatch):
        """So the refusal and the list of alternatives cannot disagree."""
        from earthlens.gee.engines import selectable_engines

        monkeypatch.setitem(ENGINES, _UnwiredEngine.name, _UnwiredEngine)
        assert _UnwiredEngine.name not in selectable_engines()
        assert "pixels" in selectable_engines()

    def test_the_abc_treats_a_late_refusal_as_a_fallback(self):
        """The conservative default, which still produces the pixels."""
        assert _UnwiredEngine().refusal_is_fatal() is False


class TestReaderRefusalTypes:
    """Which exceptions a post-routing reader refusal may arrive as."""

    def test_always_includes_the_transport_error(self, monkeypatch):
        """A discovery round trip can fail as an `OSError`."""
        from earthlens.gee import engines as engines_module

        monkeypatch.setattr(
            engines_module, "import_earthengine_reader", lambda: SimpleNamespace()
        )
        assert OSError in reader_refusal_types()

    def test_picks_up_the_readers_own_error(self, monkeypatch):
        """Resolved through the guarded import, so the extra stays optional."""
        from earthlens.gee import engines as engines_module

        monkeypatch.setattr(
            engines_module,
            "import_earthengine_reader",
            lambda: SimpleNamespace(ReaderError=_FakeReaderError),
        )
        types = reader_refusal_types()
        assert _FakeReaderError in types
        assert all(issubclass(entry, BaseException) for entry in types)

    def test_a_non_exception_attribute_is_ignored(self, monkeypatch):
        """A future release re-exporting something else must not poison the tuple."""
        from earthlens.gee import engines as engines_module

        monkeypatch.setattr(
            engines_module,
            "import_earthengine_reader",
            lambda: SimpleNamespace(ReaderError="not a class"),
        )
        assert all(issubclass(entry, BaseException) for entry in reader_refusal_types())

    def test_authentication_errors_are_not_recoverable(self, monkeypatch):
        """A credential problem must surface rather than silently fall back."""
        from earthlens.gee import engines as engines_module
        from earthlens.gee.auth import AuthenticationError

        monkeypatch.setattr(
            engines_module, "import_earthengine_reader", lambda: SimpleNamespace()
        )
        assert AuthenticationError not in reader_refusal_types()

    def test_resolves_when_the_extra_is_absent(self, monkeypatch):
        """A missing `[eedai]` install yields the transport error, not an `ImportError`.

        The call site is the expression of an `except` clause, reached only
        after a reader call has already raised. Raising there replaces the
        reader's own failure with an import failure and leaves the handler
        unable to match anything, so the resolution has to survive the extra
        being uninstalled.
        """
        from earthlens.gee import engines as engines_module

        monkeypatch.setattr(
            engines_module, "import_earthengine_reader", _raise_missing_extra
        )
        types = reader_refusal_types()
        assert OSError in types, "the transport error stands without a reader to ask"
        assert all(issubclass(entry, BaseException) for entry in types)

    def test_a_forced_reader_without_the_extra_surfaces_the_install_hint(
        self, make_gee, fake_ee, monkeypatch
    ):
        """`engine="eedai"` with no extra installed ends in the install hint.

        The end-to-end claim behind this branch: what a caller sees is the
        loader's own `ImportError`, naming the extra to install, and not a
        second failure raised while evaluating the `except` clause meant to
        handle it. The extra's absence is simulated at the import system, so
        the real guarded import runs and every module's bound copy of it
        behaves exactly as it would on a machine without pyramids-eo.
        """
        monkeypatch.setitem(sys.modules, "pyramids_eo", None)
        monkeypatch.setitem(sys.modules, "pyramids_eo.earthengine", None)
        gee = make_gee(engine="eedai")
        with pytest.raises(ImportError, match=r"pip install earthlens\[eedai\]"):
            gee.download(progress_bar=False)


class TestEeEngine:
    """Earth Engine serves anything, so it never pre-judges a request."""

    def test_never_routes_to_the_reader(self, make_gee, fake_reader):
        """`(False, None)` is how the caller routes to the Earth Engine path."""
        gee = _gee(make_gee, engine="ee")
        var_info = gee.catalog.get_dataset(ASSET)
        assert EeEngine().plan(gee, var_info, 1) == (False, None)

    def test_declines_even_a_request_the_reader_could_serve(
        self, make_gee, fake_reader
    ):
        """Choosing `ee` is a choice, not a fallback."""
        gee = _gee(make_gee, engine="ee")
        var_info = gee.catalog.get_dataset(ASSET)
        assert gee._use_eedai(var_info, 1) == (False, None)


class TestEedaiEngine:
    """Forced: a request it cannot serve raises, with one data-shaped exception."""

    def test_serves_an_eligible_raw_read(self, make_gee, fake_reader):
        """The happy path returns a servable plan."""
        gee = _gee(make_gee, engine="eedai")
        var_info = gee.catalog.get_dataset(ASSET)
        use_reader, plan = EedaiEngine().plan(gee, var_info, 1)
        assert use_reader is True
        assert plan is not None
        assert plan.can_serve

    def test_raises_on_an_ineligible_request(self, make_gee, fake_reader):
        """A `cloud_mask` needs compute the reader cannot run."""
        gee = _gee(make_gee, engine="eedai", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        engine = EedaiEngine()
        with pytest.raises(ValueError, match="cannot serve"):
            engine.plan(gee, var_info, 1)

    def test_the_ineligible_message_names_the_remedy(self, make_gee, fake_reader):
        """It should point at `engine='auto'` or `'ee'`, not just refuse."""
        gee = _gee(make_gee, engine="eedai", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        engine = EedaiEngine()
        with pytest.raises(ValueError, match="engine='auto'"):
            engine.plan(gee, var_info, 1)

    def test_an_empty_bucket_falls_through_rather_than_raising(
        self, make_gee, fake_reader
    ):
        """No scenes is a property of the data, not of the request."""
        gee = _gee(
            make_gee,
            engine="eedai",
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
        )
        fake_reader.cost = type(fake_reader.cost)(scene_count=0, min_pixel_size=5566.0)
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        assert EedaiEngine().plan(gee, var_info, 1, START, END) == (False, None)

    def test_a_refusal_after_routing_is_fatal(self):
        """A forced engine's refusal is the answer, not a reason to retry."""
        assert EedaiEngine().refusal_is_fatal() is True


class TestAutoEngine:
    """Every fallback the old `_use_eedai`/`_api` spread across two methods."""

    def test_prefers_the_reader_when_it_can_serve(self, make_gee, fake_reader):
        """The point of `auto`."""
        gee = _gee(make_gee, engine="auto")
        var_info = gee.catalog.get_dataset(ASSET)
        use_reader, plan = AutoEngine().plan(gee, var_info, 1)
        assert use_reader is True
        assert plan is not None

    def test_falls_back_when_ineligible(self, make_gee, fake_reader):
        """A `cloud_mask` declines quietly instead of raising."""
        gee = _gee(make_gee, engine="auto", cloud_mask=lambda image: image)
        var_info = gee.catalog.get_dataset(ASSET)
        assert AutoEngine().plan(gee, var_info, 1) == (False, None)

    def test_falls_back_when_the_extra_is_absent(
        self, make_gee, fake_reader, monkeypatch
    ):
        """Without `[eedai]` installed there is no reader to route to."""
        from earthlens.gee import backend as backend_module

        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)
        gee = _gee(make_gee, engine="auto")
        var_info = gee.catalog.get_dataset(ASSET)
        assert AutoEngine().plan(gee, var_info, 1) == (False, None)

    def test_falls_back_when_sizing_raises(self, make_gee, fake_reader, monkeypatch):
        """A sizing refusal routes to Earth Engine rather than aborting."""
        warnings: list[str] = []
        from earthlens.gee import engines as engines_module

        monkeypatch.setattr(
            engines_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        gee = _gee(make_gee, engine="auto")
        monkeypatch.setattr(
            type(gee),
            "_eedai_verdict",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("bounds are not finite")),
        )
        var_info = gee.catalog.get_dataset(ASSET)
        assert AutoEngine().plan(gee, var_info, 1) == (False, None)
        assert any("could not size" in w for w in warnings)

    def test_falls_back_when_the_plan_declines(
        self, make_gee, fake_reader, monkeypatch
    ):
        """A declined plan is logged and routed, not raised."""
        from earthlens.gee.plans import EnginePlan

        gee = _gee(make_gee, engine="auto")
        monkeypatch.setattr(
            type(gee), "_eedai_verdict", lambda *a, **k: EnginePlan.declined("too big")
        )
        var_info = gee.catalog.get_dataset(ASSET)
        assert AutoEngine().plan(gee, var_info, 1) == (False, None)

    def test_a_refusal_after_routing_is_not_fatal(self):
        """Under `auto`, a post-routing refusal is Earth Engine's to serve."""
        assert AutoEngine().refusal_is_fatal() is False


class TestBackendDelegates:
    """The backend holds one resolved engine instead of re-reading the string."""

    @pytest.mark.parametrize(
        ("engine", "cls"),
        [("auto", AutoEngine), ("ee", EeEngine), ("eedai", EedaiEngine)],
    )
    def test_the_backend_resolves_its_engine_once(self, make_gee, engine, cls):
        """`engine=` becomes an object at construction."""
        assert isinstance(_gee(make_gee, engine=engine)._engine, cls)

    def test_use_eedai_delegates_to_it(self, make_gee, fake_reader):
        """The old entry point now just asks the engine."""
        gee = _gee(make_gee, engine="auto")
        var_info = gee.catalog.get_dataset(ASSET)
        assert gee._use_eedai(var_info, 1) == gee._engine.plan(gee, var_info, 1)

    def test_adding_an_engine_needs_no_backend_edit(self):
        """The acceptance criterion for this seam, asserted rather than asserted of.

        A new engine registers itself and resolves, with no branch added to
        `_use_eedai` or `_api` — which is what Task 1.13 then relies on.
        """

        class _Probe(Engine):
            """A throwaway engine used only to prove registration is enough."""

            name = "probe"

            def plan(
                self, backend, var_info, band_count, bucket_start=None, bucket_end=None
            ):
                """Decline everything."""
                return False, None

        ENGINES["probe"] = _Probe
        try:
            assert isinstance(resolve_engine("probe"), _Probe)
        finally:
            del ENGINES["probe"]
