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
#: The adapter chain, which may let the loader's `ImportError` out: reaching it
#: means a read was requested. Paths are relative to the package root so a
#: same-named module elsewhere in the tree cannot inherit the exemption.
_LOADER_OWNERS = frozenset({"_eedai.py", "reader.py"})

#: Every loader-call shape the scan has to tell apart, written to a file
#: because the scan takes a path. Each function's name says which side of the
#: line it belongs on, and the meta-test below derives what must be flagged
#: from those names, so adding a shape here extends the expectation with it.
_SYNTHETIC_CALLERS = '''
from earthlens.gee._eedai import import_earthengine_reader as load


def unguarded():
    return import_earthengine_reader()


def guarded():
    try:
        return import_earthengine_reader()
    except ImportError:
        return None


def unguarded_because_the_handler_catches_the_wrong_thing():
    try:
        return import_earthengine_reader()
    except OSError:
        return None


def unguarded_through_the_module_attribute():
    return _eedai.import_earthengine_reader()


def unguarded_through_an_alias():
    return load()


def guarded_through_the_module_attribute():
    try:
        return _eedai.import_earthengine_reader()
    except ImportError:
        return None


def unguarded_because_the_handler_reraises():
    try:
        return import_earthengine_reader()
    except ImportError:
        raise


def unguarded_because_the_handler_chains():
    try:
        return import_earthengine_reader()
    except ImportError as exc:
        raise RuntimeError("no reader") from exc


def unguarded_because_the_call_runs_later():
    try:

        def inner():
            return import_earthengine_reader()

        return inner
    except ImportError:
        return None


def guarded_by_the_attribute_spelling_of_the_handler():
    try:
        return import_earthengine_reader()
    except builtins.ImportError:
        return None


def guarded_by_a_broad_handler():
    try:
        return import_earthengine_reader()
    except BaseException:
        return None


def unguarded_through_getattr():
    return getattr(_eedai, "import_earthengine_reader")()


def guarded_by_a_bare_handler():
    try:
        return import_earthengine_reader()
    except:
        return None


def guarded_by_a_tuple_handler():
    try:
        return import_earthengine_reader()
    except (OSError, ImportError):
        return None


def unguarded_because_the_tuple_handler_catches_the_wrong_things():
    try:
        return import_earthengine_reader()
    except (OSError, ValueError):
        return None


def unguarded_because_a_lambda_defers_the_call():
    try:
        deferred = lambda: import_earthengine_reader()
    except ImportError:
        deferred = None
    return deferred


def unguarded_because_a_nested_def_below_the_statement_defers_the_call():
    try:
        if _ready:

            def inner():
                return import_earthengine_reader()

        return None
    except ImportError:
        return None
'''


def _top_level_functions_holding(path: Path, lines: set[int]) -> set[str]:
    """Names of the module-level functions whose body spans any of `lines`.

    Reported by enclosing function rather than by line number so the
    expectation survives an edit to the fixture, and so a failure names the
    shape that broke instead of an integer.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and lines & set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    }


_CATCHES_MISSING_MODULE = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _exception_names(node: ast.expr | None) -> set[str]:
    """The exception names an `except` clause's expression refers to.

    Handles the attribute spelling (`builtins.ImportError`) by its last
    component, since that is the name that identifies the class.
    """
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        return {name for entry in node.elts for name in _exception_names(entry)}
    return set()


def _handles_missing_module(handler: ast.ExceptHandler) -> bool:
    """Whether this handler absorbs a missing module rather than re-raising.

    A handler that raises anything - bare `raise`, or a new exception chained
    from the original - leaves the caller facing a failure, so it does not
    make the call tolerant even though it names `ImportError`. That shape is
    real: `reader.py` uses it deliberately, because a read was requested.
    """
    if any(isinstance(node, ast.Raise) for node in ast.walk(handler)):
        return False
    if handler.type is None:
        return True
    return bool(_CATCHES_MISSING_MODULE & _exception_names(handler.type))


def _loader_names(tree: ast.Module) -> set[str]:
    """Local names bound to the loader, following `import ... as` aliases."""
    names = {_LOADER}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {
                alias.asname or alias.name
                for alias in node.names
                if alias.name == _LOADER
            }
    return names


def _is_loader_call(node: ast.AST, names: set[str]) -> bool:
    """Whether `node` calls the loader, in any spelling the package uses.

    Four shapes count: the bare name, an aliased import of it, the
    module-attribute form (`_eedai.import_earthengine_reader()`) that this
    package's own comments prescribe for optional seams, and a `getattr`
    lookup of it by string.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    if isinstance(func, ast.Attribute):
        return func.attr == _LOADER
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "getattr"
    ):
        return any(
            isinstance(argument, ast.Constant) and argument.value == _LOADER
            for argument in func.args
        )
    return False


def _walk_outside_nested_defs(node: ast.AST):
    """Yield nodes under `node` without descending into a nested definition.

    A call inside a function defined in the `try` body runs when that function
    is called, long after the `try` has exited, so the handler does not protect
    it.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _unguarded_loader_calls(path: Path) -> list[int]:
    """Line numbers where `path` calls the loader without catching its failure."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = _loader_names(tree)
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not any(
            _handles_missing_module(handler) for handler in node.handlers
        ):
            continue
        for statement in node.body:
            # A definition in the `try` body only runs when something calls
            # it, by which time the handler is long gone, so skip the whole
            # statement rather than just its nested children.
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            guarded |= {
                inner.lineno
                for inner in _walk_outside_nested_defs(statement)
                if _is_loader_call(inner, names)
            }
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if _is_loader_call(node, names) and node.lineno not in guarded
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
        scanned = sorted(_GEE_PACKAGE.rglob("*.py"))
        offenders = [
            f"{path.relative_to(_GEE_PACKAGE).as_posix()}:{line}"
            for path in scanned
            if path.relative_to(_GEE_PACKAGE).as_posix() not in _LOADER_OWNERS
            for line in _unguarded_loader_calls(path)
        ]
        assert offenders == [], f"unguarded import_earthengine_reader() at {offenders}"

    def test_the_sweep_enumerates_the_package_it_claims_to_scan(self):
        """The empty result above means nothing unless the right files were read.

        `offenders == []` is satisfied just as well by a path that resolved
        wrongly, a glob that matched nothing, or a refactor that moved the
        modules into a subpackage. Pinning the enumeration - and proving the
        exemption is load-bearing by checking the owners really do hold
        unguarded calls - is what makes the empty list evidence.
        """
        scanned = {
            path.relative_to(_GEE_PACKAGE).as_posix()
            for path in _GEE_PACKAGE.rglob("*.py")
        }
        assert {"engines.py", "backend.py", "reader.py", "_eedai.py"} <= scanned, (
            f"the scan is not reading the package it thinks it is: {sorted(scanned)[:8]}"
        )
        exempted = {
            owner: _unguarded_loader_calls(_GEE_PACKAGE / owner)
            for owner in _LOADER_OWNERS
        }
        assert all(lines for lines in exempted.values()), (
            f"the owners exemption is no longer load-bearing: {exempted}"
        )

    def test_the_scan_flags_every_call_shape_no_handler_catches(self, tmp_path):
        """The guard above passes vacuously unless the scan can actually fail.

        It asserts an empty list, which a scan that found nothing anywhere
        would also satisfy, so the production sweep says nothing on its own
        about whether an unguarded call would be caught. The fixture carries
        every spelling the package uses - bare name, aliased import, module
        attribute, `getattr` - against every handler shape that decides the
        answer: one that absorbs, re-raises, chains, catches the wrong thing,
        names nothing at all, or names a tuple that does or does not include
        the failure. It also carries the three ways a call escapes the handler
        by running later - a def statement, a def nested below one, and a
        lambda.
        """
        source = tmp_path / "callers.py"
        source.write_text(_SYNTHETIC_CALLERS, encoding="utf-8")
        flagged = set(_unguarded_loader_calls(source))
        tree = ast.parse(_SYNTHETIC_CALLERS)
        expected = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("unguarded")
        }
        assert _top_level_functions_holding(source, flagged) == expected, (
            "the scan must flag exactly the shapes the fixture names 'unguarded'"
        )
