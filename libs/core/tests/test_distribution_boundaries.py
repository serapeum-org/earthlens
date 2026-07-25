"""Guard the public API boundary between `earthlens-core` and the providers.

`earthlens-core` and the five provider distributions are versioned and released
independently, so a provider that imports an underscore-private core symbol has an
undeclared contract: any core release free to rename its internals breaks the
installed provider at runtime, in one code path, without failing core's own tests.
These tests keep that boundary explicit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Repo root, resolved from this file (libs/core/tests/ -> three levels up).
_ROOT = Path(__file__).resolve().parents[3]

#: The core packages a provider may import from.
_CORE_ROOTS = {
    "aggregate",
    "base",
    "spatial",
    "earthlens",
    "_backends",
    "grids",
    "core",
}

#: Private core modules a provider must not reach into; the public re-export is
#: `earthlens.base`, which exposes every one of these modules' supported names.
_PRIVATE_CORE_MODULES = {"earthlens.base._dates", "earthlens.base._requests"}


def _provider_sources() -> list[Path]:
    """Return every provider source file (excluding build artefacts)."""
    return sorted(
        path
        for path in _ROOT.glob("libs/providers/*/src/earthlens/**/*.py")
        if "build" not in path.parts
    )


def _core_imports(path: Path):
    """Yield `(module, imported_name)` for every `earthlens.*` import in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if not node.module.startswith("earthlens."):
                continue
            for alias in node.names:
                yield node.module, alias.name


_SOURCES = _provider_sources()
_IDS = [str(path.relative_to(_ROOT)).replace("\\", "/") for path in _SOURCES]


def _unbounded_caches(path: Path) -> list[str]:
    """Return module-level `_*CACHE*` names still assigned a plain `dict` literal.

    One entry per declaration, so a file that bounds one cache and leaves two
    beside it is still reported.
    """
    found: list[str] = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = getattr(node.targets[0], "id", None)
        if not target or "CACHE" not in target:
            continue
        if isinstance(node.value, ast.Dict) and not node.value.keys:
            found.append(target)
    return found


def _separator_replacements(path: Path) -> list[int]:
    """Return line numbers of `.replace(<sep>, "_")` calls on a path separator.

    Matched from the AST, so the quote style is irrelevant — the earlier
    double-quoted string grep missed `osm/_pbf.py`'s single-quoted
    `replace('/', '_')` entirely.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "replace" or len(node.args) != 2:
            continue
        first, second = node.args
        if not (isinstance(first, ast.Constant) and isinstance(second, ast.Constant)):
            continue
        if first.value in ("/", "\\") and second.value == "_":
            found.append(node.lineno)
    return found


def _swallows_everything(node: ast.AST) -> bool:
    """Whether `node` contains a handler that silently absorbs `Exception`.

    Structural, not textual: an earlier version tested `"pass" in unparse(...)`,
    which matched the substring inside identifiers like `password` and so
    false-positived on functions that actually re-raise. `contextlib.suppress`
    counts too — it is the same swallow written differently.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.ExceptHandler):
            names = (
                {sub.type.id}
                if isinstance(sub.type, ast.Name)
                else {
                    e.id
                    for e in getattr(sub.type, "elts", [])
                    if isinstance(e, ast.Name)
                }
            )
            if not names & {"Exception", "BaseException"}:
                continue
            if all(isinstance(s, ast.Pass) for s in sub.body):
                return True
        if isinstance(sub, ast.With):
            for item in sub.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and getattr(call.func, "attr", getattr(call.func, "id", ""))
                    == "suppress"
                ):
                    return True
    return False


def _quiet_close_lookalikes(path: Path) -> list[str]:
    """Return functions whose body is the quiet-close shape.

    The shape is "look up `close`, call it, swallow whatever it raises" — the
    duplication `earthlens.base.close_quietly` replaced. Detected from the AST so
    renaming the helper cannot hide it.

    Covers module-level *and* method definitions, `async def`, and any body length,
    since the previous version's "one or two statements, module level, `def` only"
    shape was easy to slip past. The discriminator is that `close` is the only
    method called: chc's FTP teardown calls `quit()` first and so is a genuinely
    different protocol-specific sequence, and an unrelated `try: return int(v)
    except Exception: pass` helper calls no method at all.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _swallows_everything(node):
            continue
        methods = {
            sub.func.attr
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
        }
        # `close` called, and nothing else — a lone `close` inside a
        # swallow-everything handler is the helper, whatever it is named.
        if methods == {"close"}:
            found.append(node.name)
    return found


class TestNoPrivateCoreImports:
    """No provider imports a private symbol or module from core."""

    @pytest.mark.parametrize("path", _SOURCES, ids=_IDS)
    def test_no_private_symbol_from_core(self, path):
        """A provider may only import public names out of a core package."""
        offenders = [
            f"{module}.{name}"
            for module, name in _core_imports(path)
            if module.split(".")[1] in _CORE_ROOTS
            and module.split(".")[1] != path.parent.name
            # Dunders (`__version__`) are public by convention, not internals.
            and name.startswith("_")
            and not name.startswith("__")
        ]
        assert not offenders, (
            f"{path.name} imports private core symbols {offenders}; promote them to "
            f"the core package's __all__ instead"
        )

    @pytest.mark.parametrize("path", _SOURCES, ids=_IDS)
    def test_no_private_core_module(self, path):
        """A provider imports from `earthlens.base`, not `earthlens.base._dates`."""
        offenders = [
            module
            for module, _name in _core_imports(path)
            if module in _PRIVATE_CORE_MODULES
        ]
        assert not offenders, (
            f"{path.name} imports from private core module(s) {offenders}; use the "
            f"`earthlens.base` re-export"
        )


class TestAggregatePublicSurface:
    """The primitives providers need are part of `aggregate.__all__`."""

    def test_reduce_and_window_groups_are_public(self):
        """Both primitives are importable and exported."""
        from earthlens import aggregate

        assert "reduce_time_axis" in aggregate.__all__
        assert "window_groups" in aggregate.__all__

    def test_public_names_are_importable(self):
        """The promoted names resolve to callables."""
        from earthlens.aggregate import reduce_time_axis, window_groups

        assert callable(reduce_time_axis)
        assert callable(window_groups)

    def test_private_aliases_are_gone(self):
        """The old underscore names are not left behind as a second spelling."""
        from earthlens import aggregate

        assert not hasattr(aggregate, "_reduce")
        assert not hasattr(aggregate, "_window_groups")


class TestSharedHelpersAreNotReimplemented:
    """The base helpers that exist are the ones the providers use."""

    def test_no_provider_reimplements_filename_sanitising(self):
        """Every path-sanitising site routes through `base.safe_filename`.

        The three local sanitisers only replaced path separators, so a
        Windows-illegal character (`:` in a timestamped product id) passed
        straight through into a filename.
        """
        offenders = [
            f"{str(path.relative_to(_ROOT)).replace(chr(92), '/')}:{line}"
            for path in _provider_sources()
            for line in _separator_replacements(path)
        ]
        assert not offenders, (
            f"{offenders} sanitise a path separator by hand; use "
            f"`earthlens.base.safe_filename`, which also strips the "
            f"Windows-illegal characters"
        )

    def test_no_provider_reimplements_quiet_close(self):
        """No provider re-declares the best-effort handle-release shape.

        Matched structurally, on the function's body, rather than by its name: the
        earlier name-grep version passed while two byte-equivalent copies survived
        as `_close_dataset`, certifying a property that did not hold.
        """
        offenders = [
            f"{str(path.relative_to(_ROOT)).replace(chr(92), '/')}::{name}"
            for path in _provider_sources()
            for name in _quiet_close_lookalikes(path)
        ]
        assert not offenders, (
            f"{offenders} re-implement the best-effort close shape; use "
            f"`earthlens.base.close_quietly`"
        )

    def test_close_quietly_is_exported(self):
        """The shared helper is part of the public base surface."""
        from earthlens.base import close_quietly

        assert callable(close_quietly)


class TestCatalogParseCacheIsBounded:
    """Every backend's parse cache evicts superseded generations."""

    def test_all_catalogs_use_the_bounded_cache(self):
        """Every `(path, mtime)`-keyed parse cache is a bounded one.

        Checks each cache declaration individually, not merely that the file
        mentions `CatalogParseCache` somewhere: the earlier version passed a file
        that converted one cache and left two unbounded beside it.
        """
        scope = [
            *_ROOT.glob("libs/providers/*/src/earthlens/*/catalog.py"),
            _ROOT / "libs/core/src/earthlens/base/providers.py",
        ]
        offenders = [
            f"{str(path.relative_to(_ROOT)).replace(chr(92), '/')}::{name}"
            for path in scope
            for name in _unbounded_caches(path)
        ]
        assert not offenders, (
            f"{offenders} memoise into a plain dict keyed on (path, mtime), which "
            f"retains every past generation; use "
            f"`earthlens.base.yaml_loader.CatalogParseCache`"
        )

    def test_reparsing_after_an_edit_evicts_the_old_entry(self):
        """A second mtime for one path leaves a single entry behind."""
        from earthlens.base.yaml_loader import CatalogParseCache

        cache = CatalogParseCache()
        for mtime in range(50):
            cache[("/catalog.yaml", mtime)] = f"parse {mtime}"
        assert len(cache) == 1
        assert cache[("/catalog.yaml", 49)] == "parse 49"

    def test_distinct_paths_are_retained(self):
        """Bounding is per path, so unrelated catalogs still cache."""
        from earthlens.base.yaml_loader import CatalogParseCache

        cache = CatalogParseCache()
        cache[("/a.yaml", 1)] = "a"
        cache[("/b.yaml", 1)] = "b"
        assert len(cache) == 2

    def test_non_tuple_keys_are_stored_untouched(self):
        """A cache used with a plain key still behaves like a dict."""
        from earthlens.base.yaml_loader import CatalogParseCache

        cache = CatalogParseCache()
        cache["plain"] = 1
        cache["other"] = 2
        assert cache == {"plain": 1, "other": 2}


class TestAuthDefaultPredicate:
    """`AbstractAuth.is_authenticated` defaults to the configured flag."""

    def test_default_is_false_before_configure(self):
        """A fresh auth object reports itself unauthenticated."""
        from pydantic import BaseModel

        from earthlens.base import AbstractAuth

        class _Creds(BaseModel):
            token: str

        class _Auth(AbstractAuth[_Creds]):
            def configure(self):
                self.mark_configured()

        assert _Auth(_Creds(token="x")).is_authenticated() is False

    def test_mark_configured_flips_the_default(self):
        """`mark_configured()` is what the inherited predicate reads."""
        from pydantic import BaseModel

        from earthlens.base import AbstractAuth

        class _Creds(BaseModel):
            token: str

        class _Auth(AbstractAuth[_Creds]):
            def configure(self):
                self.mark_configured()

        auth = _Auth(_Creds(token="x"))
        auth.configure()
        assert auth.is_authenticated() is True

    def test_no_provider_reimplements_the_default_predicate(self):
        """No auth class re-declares `return self._configured`."""
        import ast

        offenders = []
        for path in _ROOT.glob("libs/providers/*/src/earthlens/*/auth.py"):
            for cls in [
                n
                for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(n, ast.ClassDef)
            ]:
                for fn in cls.body:
                    if (
                        isinstance(fn, ast.FunctionDef)
                        and fn.name == "is_authenticated"
                    ):
                        body = [
                            s
                            for s in fn.body
                            if not (
                                isinstance(s, ast.Expr)
                                and isinstance(s.value, ast.Constant)
                            )
                        ]
                        if (
                            "\n".join(ast.unparse(s) for s in body)
                            == "return self._configured"
                        ):
                            offenders.append(f"{path.parent.name}.{cls.name}")
        assert not offenders, (
            f"{offenders} re-declare the inherited default; drop the override"
        )
