"""Tests for the `EedaiReader` adapter and the credential hazard it owns."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import earthlens.gee as gee_package
from earthlens.gee.auth import AuthenticationError
from earthlens.gee.reader import EedaiReader

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

_GEE_PACKAGE = Path(gee_package.__file__).parent
_LOADER = "import_earthengine_reader"

#: Three loader calls for the AST scan to tell apart: a bare one, one caught by
#: `except ImportError`, and one wrapped in a `try` that catches something else
#: entirely. Written to a file rather than parsed from a string because the scan
#: takes a path, and the expected line numbers are of this literal.
_SYNTHETIC_CALLERS = """
def unguarded():
    return import_earthengine_reader()


def guarded():
    try:
        return import_earthengine_reader()
    except ImportError:
        return None


def guarded_against_the_wrong_thing():
    try:
        return import_earthengine_reader()
    except OSError:
        return None
"""


def _handles_missing_module(handler: ast.ExceptHandler) -> bool:
    """Whether this handler catches `ImportError` or something broader."""
    if handler.type is None:
        return True
    names = (
        [entry.id for entry in handler.type.elts if isinstance(entry, ast.Name)]
        if isinstance(handler.type, ast.Tuple)
        else [handler.type.id]
        if isinstance(handler.type, ast.Name)
        else []
    )
    return bool({"ImportError", "ModuleNotFoundError", "Exception"} & set(names))


def _unguarded_loader_calls(path: Path) -> list[int]:
    """Line numbers where `path` calls the loader without catching its failure."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not any(
            _handles_missing_module(handler) for handler in node.handlers
        ):
            continue
        for statement in node.body:
            guarded |= {
                inner.lineno
                for inner in ast.walk(statement)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == _LOADER
            }
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _LOADER
        and node.lineno not in guarded
    )


@pytest.fixture(autouse=True)
def _clear_in_effect_identity():
    """Reset the process-wide identity marker between tests."""
    from earthlens.gee import reader as reader_module

    reader_module._IN_EFFECT["credential"] = None
    yield
    reader_module._IN_EFFECT["credential"] = None


class _FakeModule:
    """Stands in for `pyramids_eo.earthengine`, recording the calls it gets."""

    Window = object()

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, name):
        def _call(asset_id, **kwargs):
            self.calls.append((name, asset_id, kwargs))
            return f"{name}-result"

        return _call

    def __getattr__(self, item):
        return self._record(item)


class _InEffectProbe:
    """Records which credential was configured process-wide during the read."""

    Window = object()

    def __init__(self):
        self.seen: list[object] = []

    def from_earthengine(self, asset_id, **kwargs):
        """Capture the in-effect credential at call time."""
        from earthlens.gee import reader as reader_module

        self.seen.append(reader_module._IN_EFFECT["credential"])
        return "probed"


def _reader(credential="cred-a"):
    """An adapter bound to a fake module, so no import or network is needed."""
    adapter = EedaiReader(credential)
    adapter._module = _FakeModule()
    return adapter


class TestBinding:
    """The adapter binds the module and the credential into one object."""

    def test_passes_the_credential_on_every_entry_point(self):
        """A caller asks for a read rather than threading the credential in."""
        adapter = _reader("cred-a")
        adapter.estimate_cost("A/B", start="2020-01-01")
        adapter.from_earthengine("A/B", bands=["b1"])
        adapter.collection_from_earthengine("A/B", reducer="mean")
        for _name, _asset, kwargs in adapter._module.calls:
            assert kwargs["credentials"] == "cred-a"

    def test_each_entry_point_calls_its_upstream_name(self):
        """The adapter is a pass-through, not a rename."""
        adapter = _reader()
        adapter.estimate_cost("A/B")
        adapter.from_earthengine("A/B")
        adapter.collection_from_earthengine("A/B")
        assert [name for name, _a, _k in adapter._module.calls] == [
            "estimate_earthengine_cost",
            "from_earthengine",
            "collection_from_earthengine",
        ]

    def test_forwards_the_asset_id_and_the_read_spec(self):
        """Nothing is dropped on the way through."""
        adapter = _reader()
        adapter.from_earthengine(
            "USGS/SRTMGL1_003", bands=["elevation"], path="out.tif"
        )
        name, asset_id, kwargs = adapter._module.calls[0]
        assert asset_id == "USGS/SRTMGL1_003"
        assert kwargs["bands"] == ["elevation"]
        assert kwargs["path"] == "out.tif"

    def test_exposes_the_window_class(self):
        """Callers build a read spec without importing the reader themselves."""
        adapter = _reader()
        assert adapter.window_type is _FakeModule.Window

    def test_the_module_is_imported_once_and_then_cached(self, monkeypatch):
        """Re-importing per call is what the adapter exists to stop."""
        from earthlens.gee import reader as reader_module

        module = _FakeModule()
        imports: list[int] = []
        monkeypatch.setattr(
            reader_module,
            "import_earthengine_reader",
            lambda: imports.append(1) or module,
        )
        adapter = EedaiReader("cred-a")
        assert adapter.module is module
        assert adapter.module is module
        assert imports == [1]

    def test_the_window_class_triggers_the_lazy_import(self, monkeypatch):
        """Asking for `Window` is enough; no separate resolve step is needed."""
        from earthlens.gee import reader as reader_module

        module = _FakeModule()
        monkeypatch.setattr(reader_module, "import_earthengine_reader", lambda: module)
        assert EedaiReader("cred-a").window_type is _FakeModule.Window

    def test_a_read_resolves_the_module_through_the_lazy_import(self, monkeypatch):
        """Nothing has to pre-set the module for a read to work."""
        from earthlens.gee import reader as reader_module

        module = _FakeModule()
        monkeypatch.setattr(reader_module, "import_earthengine_reader", lambda: module)
        adapter = EedaiReader("cred-a")
        assert adapter.from_earthengine("A/B") == "from_earthengine-result"
        assert module.calls[0][1] == "A/B"

    def test_repr_says_whether_a_credential_is_bound_without_printing_it(self):
        """Key material must never reach a log line."""
        assert repr(_reader("secret-key-material")) == "EedaiReader(pinned)"
        assert repr(_reader(None)) == "EedaiReader(adc)"
        assert "secret" not in repr(_reader("secret-key-material"))


class TestProcessGlobalHazard:
    """pyramids-eo configures GDAL's EEDAI credentials process-globally."""

    def test_a_concurrent_read_under_another_identity_is_refused(self):
        """Overlapping reads under two identities would read as whichever won."""
        from earthlens.gee import reader as reader_module

        reader_module._IN_EFFECT["credential"] = "cred-b"
        adapter = _reader("cred-a")
        with pytest.raises(AuthenticationError, match="in flight under a different"):
            adapter.from_earthengine("A/B")

    def test_the_refusal_says_what_to_do_instead(self):
        """It should name the remedy, not just refuse."""
        from earthlens.gee import reader as reader_module

        reader_module._IN_EFFECT["credential"] = "cred-b"
        adapter = _reader("cred-a")
        with pytest.raises(AuthenticationError, match="sequentially"):
            adapter.estimate_cost("A/B")

    def test_a_concurrent_read_under_the_same_identity_is_fine(self):
        """Only a genuine identity conflict is an error."""
        from earthlens.gee import reader as reader_module

        reader_module._IN_EFFECT["credential"] = "cred-a"
        assert _reader("cred-a").from_earthengine("A/B") == "from_earthengine-result"

    def test_sequential_reads_under_different_identities_are_fine(self):
        """The marker is released after each read, so the next may differ."""
        assert _reader("cred-a").from_earthengine("A/B")
        assert _reader("cred-b").from_earthengine("A/B")

    def test_the_credential_is_in_effect_during_the_read(self):
        """The marker has to be set while reading, not only checked beforehand."""
        adapter = EedaiReader("cred-a")
        probe = _InEffectProbe()
        adapter._module = probe
        assert adapter.from_earthengine("A/B") == "probed"
        assert probe.seen == ["cred-a"]

    def test_the_marker_returns_to_the_previous_identity_not_to_none(self):
        """A nested read must restore what was in effect, not clear it."""
        from earthlens.gee import reader as reader_module

        reader_module._IN_EFFECT["credential"] = "cred-a"
        _reader("cred-a").from_earthengine("A/B")
        assert reader_module._IN_EFFECT["credential"] == "cred-a"

    def test_the_marker_is_released_even_when_the_read_raises(self):
        """A failed read must not leave the process wedged to one identity."""
        from earthlens.gee import reader as reader_module

        adapter = _reader("cred-a")

        def _boom(asset_id, **kwargs):
            raise RuntimeError("upstream blew up")

        adapter._module.from_earthengine = _boom
        with pytest.raises(RuntimeError, match="upstream blew up"):
            adapter.from_earthengine("A/B")
        assert reader_module._IN_EFFECT["credential"] is None


class TestFromServiceKey:
    """Building the adapter, and the error contract it keeps."""

    def test_warns_when_no_key_resolves(self, monkeypatch):
        """ADC may be a different identity than the Earth Engine half uses."""
        from earthlens.gee import backend as backend_module
        from earthlens.gee import reader as reader_module

        warnings: list[str] = []
        monkeypatch.setattr(
            reader_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        monkeypatch.setattr(backend_module, "credentials_for", lambda key: "adc-cred")
        EedaiReader.from_service_key(None)
        assert any("Application Default Credentials" in w for w in warnings)

    def test_does_not_warn_when_a_key_resolves(self, monkeypatch):
        """A pinned identity needs no notice."""
        from earthlens.gee import backend as backend_module
        from earthlens.gee import reader as reader_module

        warnings: list[str] = []
        monkeypatch.setattr(
            reader_module.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
        )
        monkeypatch.setattr(backend_module, "credentials_for", lambda key: "cred")
        EedaiReader.from_service_key("key.json")
        assert warnings == []

    def test_a_build_failure_becomes_an_authentication_error(self, monkeypatch):
        """The backend's error contract, not pyramids-eo's exception type."""
        from earthlens.gee import backend as backend_module

        def _boom(key):
            raise RuntimeError("bad key")

        monkeypatch.setattr(backend_module, "credentials_for", _boom)
        with pytest.raises(AuthenticationError, match="could not build"):
            EedaiReader.from_service_key("key.json")

    def test_a_missing_extra_still_raises_importerror(self, monkeypatch):
        """The install hint must not be swallowed into an auth error."""
        from earthlens.gee import backend as backend_module

        def _missing(key):
            raise ImportError("install earthlens[eedai]")

        monkeypatch.setattr(backend_module, "credentials_for", _missing)
        with pytest.raises(ImportError, match="eedai"):
            EedaiReader.from_service_key("key.json")

    def test_binds_the_built_credential_to_the_adapter(self, monkeypatch):
        """The whole point of the adapter is carrying the credential with it."""
        from earthlens.gee import backend as backend_module

        monkeypatch.setattr(backend_module, "credentials_for", lambda key: ("c", key))
        adapter = EedaiReader.from_service_key("key.json")
        assert adapter.credential == ("c", "key.json")

    def test_the_module_is_not_resolved_at_build_time(self, monkeypatch):
        """Building the adapter must not pull in the optional dependency."""
        from earthlens.gee import backend as backend_module

        monkeypatch.setattr(backend_module, "credentials_for", lambda key: "cred")
        assert EedaiReader.from_service_key("key.json")._module is None

    def test_available_reads_the_backend_seam(self, monkeypatch):
        """So patching the backend binding still simulates a missing extra."""
        from earthlens.gee import backend as backend_module

        monkeypatch.setattr(backend_module, "eedai_available", lambda: False)
        assert EedaiReader.available() is False


class TestBackendIntegration:
    """The backend holds one adapter and drops it when the identity may change."""

    def test_the_adapter_is_built_once(self, make_gee, fake_reader):
        """Rebuilding it per bucket would scatter transient key files."""
        gee = make_gee()
        first = gee._eedai_reader()
        assert gee._eedai_reader() is first

    def test_the_credential_accessor_still_works(self, make_gee, fake_reader):
        """`_eedai_credentials` is the pre-Epic-1 entry point callers use."""
        gee = make_gee()
        assert gee._eedai_credentials() is gee._eedai_reader().credential

    def test_reauthenticating_drops_the_adapter(self, make_gee, fake_reader):
        """Otherwise a new key would keep reading as the previous identity."""
        gee = make_gee()
        first = gee._eedai_reader()
        gee.authenticate(service_account="other@x.iam", service_key="other.json")
        assert gee._eedai_reader() is not first


class TestTheBackendReadsThroughTheAdapter:
    """The guard is only real if production calls go through it."""

    def test_the_cost_query_goes_through_the_adapter(self, make_gee, fake_reader):
        """So the scene-discovery call holds the lock like any other read."""
        import datetime as dt

        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        assert fake_reader.cost_calls, "the scene-cost query never reached the reader"

    def test_the_adapter_injects_the_credential_on_a_cost_query(
        self, make_gee, fake_reader
    ):
        """The call site no longer passes one, so the adapter must supply it."""
        import datetime as dt

        gee = make_gee(
            variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
            start="2020-06-01",
            end="2020-06-30",
            scale=5566.0,
        )
        var_info = gee.catalog.get_dataset("UCSB-CHG/CHIRPS/DAILY")
        gee._eedai_collection_fits(
            var_info, 1, dt.datetime(2020, 6, 1), dt.datetime(2020, 7, 1)
        )
        _asset_id, kwargs = fake_reader.cost_calls[0]
        assert kwargs["credentials"] is not None

    def test_the_adapter_injects_the_credential_on_a_pixel_read(
        self, make_gee, fake_reader
    ):
        """The same for the read that actually fetches pixels."""
        make_gee().download(progress_bar=False)
        _asset_id, kwargs = fake_reader.calls[0]
        assert kwargs["credentials"] is not None

    def test_the_backend_no_longer_imports_the_module_itself(self):
        """The import moved to the adapter, which is what owns the lock."""
        import inspect

        from earthlens.gee import backend as backend_module

        source = inspect.getsource(backend_module)
        assert "import_earthengine_reader()" not in source

    def test_every_loader_call_outside_the_adapter_tolerates_a_missing_extra(self):
        """A caller of the guarded loader either owns it or survives its absence.

        The adapter chain may let the `ImportError` out, because reaching it
        means a read was requested. Anywhere else the call sits on a path that
        has to keep working without the `[eedai]` extra, and an unguarded one
        there is how `engines.reader_refusal_types()` came to raise from inside
        an `except` clause. A fourth call site now has to make that choice
        explicitly instead of inheriting the bug.
        """
        owners = {"_eedai.py", "reader.py"}
        offenders = [
            f"{path.name}:{line}"
            for path in sorted(_GEE_PACKAGE.glob("*.py"))
            if path.name not in owners
            for line in _unguarded_loader_calls(path)
        ]
        assert offenders == [], f"unguarded import_earthengine_reader() at {offenders}"

    def test_the_scan_flags_a_call_no_handler_catches(self, tmp_path):
        """The guard above passes vacuously unless the scan can actually fail.

        It asserts an empty list, which a scan that found nothing anywhere
        would also satisfy, so the production sweep says nothing on its own
        about whether an unguarded call would be caught.
        """
        source = tmp_path / "callers.py"
        source.write_text(_SYNTHETIC_CALLERS, encoding="utf-8")
        assert _unguarded_loader_calls(source) == [3, 15], (
            "the scan must flag the bare call and the one whose handler catches "
            "the wrong thing, and spare the one caught by `except ImportError`"
        )
