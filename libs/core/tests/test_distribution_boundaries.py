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
    # `biodiversity` and `cli` ship from core too. Leaving them out did not
    # merely under-cover the rule — it exempted them, so osm and overture
    # imported `earthlens.biodiversity._helpers` (a private module) for as long
    # as this guard has existed, and it reported a clean boundary.
    "biodiversity",
    "cli",
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


#: GDAL and its siblings belong to pyramids, never to earthlens. `osgeo` is not
#: even importable on its own here -- pyramids vendors it and puts it on the
#: path as a side effect of being imported -- so a bare import is both a layering
#: break and a latent `ModuleNotFoundError`.
_BANNED_GIS_MODULES = frozenset({"osgeo", "gdal", "ogr", "osr"})

#: The one place allowed to reach for GDAL, and why. `gdal_module()` reads a CF
#: `time` coordinate's `units` through the multidim API, because under the HDF5
#: driver (which GDAL picks for any NetCDF-4 over `/vsicurl` on Windows) that
#: attribute never reaches `meta_data.get_dimension().attrs`, and
#: `MDArray.GetUnit()` is the only route to the epoch.
#:
#: Blocked on serapeum-org/pyramids#1078. When that lands, delete the import in
#: `_helpers.py` *and* this entry -- the assertion below is a subset check, so
#: an empty allowance keeps passing.
_GDAL_ALLOWED = frozenset({"libs/providers/hazards/src/earthlens/jrc/_helpers.py"})


def _earthlens_sources() -> list[Path]:
    """Return every earthlens Python file the GDAL rule governs.

    Covers the test and tooling trees as well as shipped source: this very
    branch had to hand-convert `osgeo` out of two FDSN tests, and a rule that
    only watches `src/` would let the next one back in unnoticed.
    """
    return sorted(
        path
        for pattern in (
            "libs/core/src/earthlens/**/*.py",
            "libs/providers/*/src/earthlens/**/*.py",
            "libs/core/tests/**/*.py",
            "libs/providers/*/tests/**/*.py",
            "tools/**/*.py",
        )
        for path in _ROOT.glob(pattern)
        if "build" not in path.parts and "__pycache__" not in path.parts
    )


def _gis_imports(path: Path) -> list[tuple[int, str]]:
    """Return `(lineno, what)` for every banned GIS import in `path`.

    Walks the AST rather than grepping, so `from osgeo.gdal import Translate`,
    `import osgeo.gdal as g`, multi-line `from ... import (...)` continuations
    and the dynamic forms are all caught. "Dynamic" means a STRING LITERAL
    argument: `importlib.import_module("osgeo")`, a bare `import_module("osgeo")`
    pulled in with `from importlib import import_module`, and
    `__import__("osgeo")`. A name computed at runtime is out of reach of a static
    walk, and pretending otherwise would overstate what this guard proves.
    """
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in _BANNED_GIS_MODULES:
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # `node.level` > 0 is a relative import, which resolves to a sibling
            # module inside earthlens -- a local `osgeo.py` would be odd, but it
            # is not GDAL, so flagging it would be a false positive.
            if (
                not node.level
                and (node.module or "").split(".", 1)[0] in _BANNED_GIS_MODULES
            ):
                found.append((node.lineno, f"from {node.module}"))
        elif isinstance(node, ast.Call):
            # Match on the callee's NAME, not on the node shape: an attribute
            # call (`importlib.import_module`) and a plain name call (a bare
            # `import_module` imported from importlib, or `__import__`) reach
            # the same place, and keying off the shape silently missed the
            # bare form.
            func = node.func
            if isinstance(func, ast.Attribute):
                called: str | None = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            else:
                called = None
            target = None
            if (
                called in {"import_module", "__import__"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                target = node.args[0].value
            if target and target.split(".", 1)[0] in _BANNED_GIS_MODULES:
                found.append((node.lineno, f"dynamic:{target}"))
    return found


def _is_banned_gis_import(node: ast.AST) -> bool:
    """Return whether `node` is an import statement pulling in a banned GIS module."""
    if isinstance(node, ast.Import):
        return any(
            alias.name.split(".", 1)[0] in _BANNED_GIS_MODULES for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        return (
            not node.level
            and (node.module or "").split(".", 1)[0] in _BANNED_GIS_MODULES
        )
    return False


def _imports_pyramids(node: ast.AST) -> bool:
    """Return whether `node` imports pyramids in any form that vendors osgeo."""
    if isinstance(node, ast.Import):
        return any(alias.name.split(".", 1)[0] == "pyramids" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        # `from pyramids import dataset` imports the PACKAGE first and runs its
        # __init__, so it puts the vendored osgeo on the path exactly as a plain
        # `import pyramids` does. Rejecting this form would fail correct code.
        return (node.module or "").split(".", 1)[0] == "pyramids"
    return False


_SOURCES = _provider_sources()
_IDS = [str(path.relative_to(_ROOT)).replace("\\", "/") for path in _SOURCES]


def _unbounded_caches(path: Path) -> list[str]:
    """Return cache-looking names still bound to a fresh unbounded `dict`.

    One entry per declaration, so a file that bounds one cache and leaves two
    beside it is still reported. Matches `{}` and `dict()`, any capitalisation of
    "cache", every target of a tuple/multiple assignment, and declarations nested
    inside a class — the first version saw only module-level `_*CACHE*: ... = {}`
    and would have missed each of those.
    """
    found: list[str] = []

    def is_empty_dict(value: ast.expr | None) -> bool:
        if isinstance(value, ast.Dict):
            return not value.keys
        return (
            isinstance(value, ast.Call)
            and getattr(value.func, "id", "") == "dict"
            and not value.args
            and not value.keywords
        )

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets: list[ast.expr] = []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not is_empty_dict(node.value):
            continue
        for target in targets:
            names = (
                [e for e in target.elts]
                if isinstance(target, (ast.Tuple, ast.List))
                else [target]
            )
            for name in names:
                ident = getattr(name, "id", None)
                if ident and "cache" in ident.lower():
                    found.append(ident)
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
        # `contextlib.suppress(...)` is itself an attribute call, so it landed
        # in this set and the `== {"close"}` test rejected the very form the
        # docstring says is caught. It is the swallow, not a second method the
        # function calls, so it is discounted here.
        methods.discard("suppress")
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

    def test_timeout_is_exported(self):
        """The public timeout alias is re-exported from the base package."""
        from earthlens.base import Timeout, __all__
        from earthlens.base.http import Timeout as HttpTimeout

        assert Timeout is HttpTimeout
        assert "Timeout" in __all__


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


class TestCatalogLoaderAdoption:
    """ARC-8: a catalog must not re-implement the shared cache-key dance."""

    def _catalog_sources(self):
        """Yield `(path, source)` for every provider `catalog.py`."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/catalog.py")):
            yield path, path.read_text(encoding="utf-8")

    def test_catalog_modules_were_scanned(self):
        """Guard the guard: the glob must actually find the catalogs."""
        found = [p.parent.name for p, _ in self._catalog_sources()]
        assert len(found) > 40, f"only scanned {len(found)} catalogs: {found[:5]}"

    def test_no_catalog_rebuilds_the_mtime_cache_key(self):
        """No loader hand-rolls `st_mtime_ns` — they go through load_catalog.

        Each hand-rolled copy drifts on its own: the size half of the key, the
        missing-file error, and whether a cache hit hands out a shared mutable
        object were all inconsistent before the migration.
        """
        import ast as ast_module

        offenders = []
        for path, source in self._catalog_sources():
            # A mention in a comment or docstring is fine — only a real
            # attribute read means the loader is computing its own key.
            reads_mtime = any(
                isinstance(node, ast_module.Attribute) and node.attr == "st_mtime_ns"
                for node in ast_module.walk(ast_module.parse(source))
            )
            if reads_mtime:
                offenders.append(path.parent.name)
        # All 48 now route through load_catalog, including the five sharded
        # catalogs that used to carry extra work of their own: chc expands
        # regions across two layouts, hdx / earthdata keep a huge `available_*`
        # index in a sibling JSON, and gee / jaxa merge per-family shards. The
        # list stays here (rather than the assertion becoming `not offenders`)
        # so re-introducing a hand-rolled key names the catalog that did it.
        assert sorted(offenders) == [], (
            "these catalogs re-implement the mtime cache key instead of using "
            f"earthlens.base.catalog_source.load_catalog: {sorted(offenders)}"
        )


class TestAggregateCapabilityIsCentral:
    """ARC-1: no backend re-implements the `aggregate=` refusal."""

    def _backend_sources(self):
        """Yield `(path, source)` for every provider `backend.py`."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            yield path, path.read_text(encoding="utf-8")

    def test_backends_were_scanned(self):
        """Guard the guard: the glob must actually find the backends."""
        found = [p.parent.name for p, _ in self._backend_sources()]
        assert len(found) > 40, f"only scanned {len(found)}: {found[:5]}"

    def test_no_backend_hand_rolls_the_refusal(self):
        """The policy lives once, in `_refuse_unsupported_aggregate`.

        40 backends used to each declare `aggregate=` and raise their own
        `NotImplementedError`, so the rule was written 40 times and the argument
        sat in the signature of backends it meant nothing to. A backend that
        cannot aggregate now declares nothing; one that can sets
        `SUPPORTS_AGGREGATE` and may add `AGGREGATE_REFUSAL_REASON`.
        """
        import re

        # A *refusal*, not any use of the condition: an implementer legitimately
        # writes `if aggregate is not None:` to branch into the reducer. What
        # must not come back is that branch raising NotImplementedError.
        refusal = re.compile(
            r"if aggregate is not None:\s*\n\s+raise NotImplementedError", re.M
        )
        offenders = [
            path.parent.name
            for path, source in self._backend_sources()
            if refusal.search(source)
        ]
        assert offenders == [], (
            "these backends re-implement the aggregate= refusal instead of "
            f"declaring SUPPORTS_AGGREGATE: {offenders}"
        )

    def test_only_real_implementers_declare_support(self):
        """A backend claiming `SUPPORTS_AGGREGATE` must actually use the argument.

        The reverse of the check above: declaring the capability without wiring
        the reducer would silently accept `aggregate=` and ignore it, which is
        worse than refusing it.
        """
        import ast as ast_module

        liars = []
        for path, source in self._backend_sources():
            # Read the declaration from the AST, not the text: this very test
            # file's own explanatory comments contain the literal
            # `SUPPORTS_AGGREGATE = True`, and so does drought's comment saying
            # it deliberately does *not* declare it.
            declares = False
            for node in ast_module.walk(ast_module.parse(source)):
                # `SUPPORTS_AGGREGATE: bool = True` is an `AnnAssign`, which an
                # `Assign`-only scan would skip — the declaration would be live
                # and this guard blind to it.
                if isinstance(node, ast_module.Assign):
                    names = [getattr(t, "id", "") for t in node.targets]
                    value = node.value
                elif isinstance(node, ast_module.AnnAssign):
                    names = [getattr(node.target, "id", "")]
                    value = node.value
                else:
                    continue
                if (
                    "SUPPORTS_AGGREGATE" in names
                    and getattr(value, "value", None) is True
                ):
                    declares = True
                    break
            if not declares:
                continue
            # Read the AST, not the text. A substring search is fooled by a
            # backend that only *names* the aggregator in its error message —
            # cmip6 did exactly that ("reduce them separately with
            # ...aggregate_netcdf"), and a text scan classified it as an
            # implementer.
            uses_it = False
            for node in ast_module.walk(ast_module.parse(source)):
                if isinstance(node, ast_module.Name) and node.id == "aggregate":
                    uses_it = True
                    break
                if isinstance(node, ast_module.Attribute) and node.attr.startswith(
                    "_aggregate"
                ):
                    uses_it = True
                    break
            # Merely *naming* `aggregate` is not using it. drought declared
            # support while its only references were
            # `if aggregate is not None: raise NotImplementedError` — the name
            # appears, so a name-scan passed it, and the central gate then
            # waved the call through on a declaration that was false. Consuming
            # it means passing it to a call or storing it (openeo and worldpop
            # assign it to an instance attribute rather than forwarding it
            # directly, and both are genuine implementers).
            #
            # Note this cannot be "declares and also raises NotImplementedError
            # about aggregate": s3 legitimately reduces NetCDF and refuses only
            # its COG datasets, and that partial support is not a lie.
            consumes_it = False
            for node in ast_module.walk(ast_module.parse(source)):
                if isinstance(node, ast_module.Call):
                    passed = list(node.args) + [kw.value for kw in node.keywords]
                    if any(
                        isinstance(arg, ast_module.Name) and arg.id == "aggregate"
                        for arg in passed
                    ):
                        consumes_it = True
                        break
                if isinstance(node, (ast_module.Assign, ast_module.AnnAssign)):
                    value = node.value
                    if isinstance(value, ast_module.Name) and value.id == "aggregate":
                        consumes_it = True
                        break
            if not (uses_it and consumes_it):
                liars.append(path.parent.name)
        assert liars == [], (
            f"these declare SUPPORTS_AGGREGATE but never use aggregate: {liars}"
        )


class TestDownloadSignatureContract:
    """ARC-2: `download` has one shape every backend satisfies."""

    def _download_defs(self):
        """Yield `(backend_name, ast.FunctionDef)` for each `download` override."""
        import ast as ast_module
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            tree = ast_module.parse(path.read_text(encoding="utf-8"))
            for node in ast_module.walk(tree):
                if isinstance(node, ast_module.FunctionDef) and node.name == "download":
                    yield path.parent.name, node

    def test_download_overrides_were_found(self):
        """Guard the guard: the walk must actually find the overrides."""
        names = [name for name, _ in self._download_defs()]
        assert len(names) > 40, f"only found {len(names)}: {names[:5]}"

    def test_every_download_takes_progress_bar_first(self):
        """`progress_bar` is the one universal argument, so it comes first.

        The base used to declare `download(self)` while every override took two
        to five arguments. Nothing could catch drift, and the argument order
        varied. Pinning the first parameter keeps a positional
        `download(False)` meaning the same thing on all 48.
        """
        offenders = []
        for name, node in self._download_defs():
            args = [a.arg for a in node.args.args]
            if args[:2] != ["self", "progress_bar"]:
                offenders.append(f"{name}: {args}")
        assert offenders == [], (
            f"download must start with (self, progress_bar, ...): {offenders}"
        )

    def test_capability_arguments_are_annotated(self):
        """A shared argument carries the same annotation everywhere it appears.

        `aggregate=` was spelled four ways across the tree
        (`AggregationConfig | None`, `Any`, `Any | None`, and unannotated), which
        is how a shared contract stops being checkable.
        """
        import ast as ast_module

        expected = {
            "aggregate": "AggregationConfig | None",
            "progress_bar": "bool",
            "errors": "str",
            "force": "bool",
        }
        offenders = []
        for name, node in self._download_defs():
            for arg in node.args.args + node.args.kwonlyargs:
                want = expected.get(arg.arg)
                if want is None:
                    continue
                got = ast_module.unparse(arg.annotation) if arg.annotation else "(none)"
                if got != want:
                    offenders.append(f"{name}.{arg.arg}: {got} (want {want})")
        assert offenders == [], f"annotation drift: {offenders}"


class TestCatalogAutoloadIsShared:
    """ARC-4: the load-if-empty rule lives once, in `AbstractCatalog`."""

    def _catalog_sources(self):
        """Yield `(backend_name, source)` for every provider `catalog.py`."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/catalog.py")):
            yield path.parent.name, path.read_text(encoding="utf-8")

    def test_catalogs_were_scanned(self):
        """Guard the guard: the glob must find the catalogs."""
        names = [name for name, _ in self._catalog_sources()]
        assert len(names) > 40, f"only scanned {len(names)}: {names[:5]}"

    def test_most_catalogs_use_the_shared_autoload(self):
        """The simple shape declares `_autoload`, not its own `model_post_init`.

        A catalog with real post-init work (an alias index, a per-instance
        `OUTPUT_KIND`, a second cache) still overrides `model_post_init` — the
        point is that the *shared* rule is not restated alongside it. This pins
        the ratio so the duplication cannot creep back one catalog at a time.
        """
        autoload, bespoke = [], []
        for name, source in self._catalog_sources():
            if "def _autoload" in source:
                autoload.append(name)
            elif "def model_post_init" in source:
                bespoke.append(name)
        # 32 today. Pinned just under, so losing a handful is a failure
        # rather than a floor that a halved population still clears.
        assert len(autoload) >= 30, (
            f"only {len(autoload)} catalogs use the shared autoload; "
            f"still hand-rolling post-init: {bespoke}"
        )

    def test_no_catalog_reimplements_the_load_if_empty_guard(self):
        """A converted catalog must not also carry the `if not self.datasets` test."""
        offenders = [
            name
            for name, source in self._catalog_sources()
            if "def _autoload" in source and "if not self.datasets" in source
        ]
        assert offenders == [], (
            f"these declare _autoload and still guard on an empty catalog "
            f"themselves, so the rule is applied twice: {offenders}"
        )


class TestRateLimitDeclarationIsWired:
    """ARC-10: a declared `MIN_REQUEST_INTERVAL` must reach an `HttpClient`."""

    def _backend_sources(self):
        """Yield `(backend_name, source)` for every provider `backend.py`."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            yield path.parent.name, path.read_text(encoding="utf-8")

    def test_backends_were_scanned(self):
        """Guard the guard: the glob must find the backends."""
        names = [name for name, _ in self._backend_sources()]
        assert len(names) > 40, f"only scanned {len(names)}"

    def test_a_declared_interval_is_passed_to_a_client(self):
        """Declaring a limit without passing it would be decorative.

        `min_interval` sat on `HttpClient` unused by every call site, which is how
        a feature, its lock and its tests end up carried for nothing. A backend
        that now declares a limit has to hand it to the client that makes the
        requests.
        """
        offenders = [
            name
            for name, source in self._backend_sources()
            if "MIN_REQUEST_INTERVAL: float = " in source
            and "min_interval=self.MIN_REQUEST_INTERVAL" not in source
        ]
        assert offenders == [], (
            f"these declare MIN_REQUEST_INTERVAL but never pass it to an "
            f"HttpClient, so nothing paces their requests: {offenders}"
        )

    def test_at_least_one_backend_paces_itself(self):
        """The mechanism is live, not merely available."""
        wired = [
            name
            for name, source in self._backend_sources()
            if "min_interval=self.MIN_REQUEST_INTERVAL" in source
        ]
        assert wired, (
            "no backend passes a rate limit to its client; min_interval is dead "
            "code again"
        )


class TestDocstringSectionsHaveBodies:
    """No Google-style section header is left without content.

    A sweep that deletes the last entry from a `Raises:` block leaves the header
    behind, which renders as an empty section in the docs. That happened here:
    removing the `aggregate=` refusals orphaned 22 `Raises:` headers, and nothing
    objected — ruff and mypy do not read docstring structure.
    """

    def _source_files(self):
        """Yield every provider and core source file, skipping build artefacts."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3]
        for pattern in (
            "libs/providers/*/src/earthlens/**/*.py",
            "libs/core/src/earthlens/**/*.py",
        ):
            for path in sorted(root.glob(pattern)):
                if "build" not in path.parts:
                    yield path

    def test_files_were_scanned(self):
        """Guard the guard: the globs must find the sources."""
        assert len(list(self._source_files())) > 200

    def test_no_orphaned_section_headers(self):
        """Every `Args:` / `Returns:` / `Raises:` / `Yields:` has an indented body."""
        import re

        orphans = []
        for path in self._source_files():
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                match = re.match(r"^(\s+)(Raises|Args|Returns|Yields):\s*$", line)
                if not match:
                    continue
                indent = len(match.group(1))
                nxt = lines[index + 1] if index + 1 < len(lines) else ""
                body_indent = len(nxt) - len(nxt.lstrip()) if nxt.strip() else -1
                if (
                    not nxt.strip()
                    or nxt.strip().startswith('"""')
                    or body_indent <= indent
                ):
                    orphans.append(f"{path.name}:{index + 1} {match.group(2)}:")
        assert orphans == [], f"empty docstring sections: {orphans}"


class TestLimitIsBoundedNotTrimmed:
    """ARC-3: a backend that accepts `limit=` must let it stop the work.

    The failure this guards against is a `limit=` that reads as a cap but is
    really a slice: the backend fetches every product, concatenates, and returns
    the first `n` rows. That bounds the *return value* while the memory and the
    request count stay unbounded, which is the opposite of what a caller passing
    a cap is asking for. A conforming backend validates the cap through
    `check_limit` and routes its assembly through one of the bounding helpers,
    all of which consume their input lazily.
    """

    #: The helpers that consume fragments lazily and stop at the cap.
    #: `_search_fetch_each` qualifies because the base composition itself runs
    #: through `_take_limited` on both its paths (pinned by
    #: `TestSearchFetchEachIsBounded`), so a backend built on it inherits the
    #: bound without naming a helper of its own.
    BOUNDING_HELPERS = (
        "_take_limited",
        "_fetch_limited",
        "iter_download",
        "_search_fetch_each",
    )

    def _pushes_limit_to_the_service(self, source: str) -> bool:
        """Whether the cap is handed to the provider's own query.

        The strongest bound available: the service never sends the rows past
        the cap, so there is nothing to trim client-side and no helper is
        needed. fdsn does this — `limit=self._request_limit` goes into the FDSN
        `get_events` call. Detected structurally (a `limit=` keyword bound to a
        cap attribute, on a call that is not one of the bounding helpers) so a
        backend cannot claim it with a comment.

        `_request_limit` is the provider-side name; `_limit` is the base's
        client-side total. Both count here, because either can legitimately be
        the value a backend forwards to its own query.
        """
        cap_attributes = {"self._limit", "self._request_limit"}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in self.BOUNDING_HELPERS:
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "limit"
                    and ast.unparse(keyword.value) in cap_attributes
                ):
                    return True
        return False

    def _backends_accepting_limit(self):
        """Yield `(name, source)` for each backend that takes a `limit`.

        Both entry points count: a cap can arrive as a `download(limit=)`
        keyword (the facade forwards `**kwargs` to it) or as a constructor
        argument held for the whole session, as openaq does. A backend that
        takes one anywhere owes the caller the same guarantee.
        """
        root = Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.FunctionDef)
                    and node.name in {"download", "__init__"}
                ):
                    continue
                args = [a.arg for a in node.args.args + node.args.kwonlyargs]
                if "limit" in args:
                    yield path.parent.name, source
                    break

    def test_some_backends_accept_a_limit(self):
        """Guard the guard: the scan must actually find the converted backends."""
        found = [name for name, _ in self._backends_accepting_limit()]
        # 15 backends take a cap today; a floor of 8 was set before most
        # were converted and would now pass with half of them regressed.
        assert len(found) >= 14, f"only found {found}"

    def test_an_accepted_limit_is_validated(self):
        """An unchecked cap lets `limit=0` or `limit=-1` through to the fetch."""
        offenders = [
            name
            for name, source in self._backends_accepting_limit()
            if "check_limit(" not in source
        ]
        assert offenders == [], (
            f"these accept limit= without validating it through check_limit, so "
            f"a zero or negative cap reaches the fetch loop: {offenders}"
        )

    def test_an_accepted_limit_actually_bounds_the_work(self):
        """Without a lazy helper or a server-side push, a cap is a post-hoc slice."""
        offenders = [
            name
            for name, source in self._backends_accepting_limit()
            if not any(helper in source for helper in self.BOUNDING_HELPERS)
            and not self._pushes_limit_to_the_service(source)
        ]
        assert offenders == [], (
            f"these accept limit= but neither route through "
            f"{self.BOUNDING_HELPERS} nor push the cap into the provider query, "
            f"so every product is still fetched and the cap only trims the "
            f"returned value: {offenders}"
        )

    def test_the_server_side_form_is_recognised_and_not_a_free_pass(self):
        """Guard the guard: the escape hatch must be structural, not textual.

        A backend that merely mentions `limit` in a comment, or passes its own
        page size, must not read as bounded — otherwise the exemption that lets
        fdsn through would excuse every unbounded backend too.
        """
        assert self._pushes_limit_to_the_service(
            "client.get_events(starttime=t, limit=self._limit)"
        )
        assert not self._pushes_limit_to_the_service(
            "# we pass limit=self._limit somewhere\nclient.get(limit=self._page_limit)"
        )
        assert not self._pushes_limit_to_the_service(
            "self._take_limited(frames, limit=self._limit)"
        )


class TestProviderCapsDoNotShadowTheBaseAttribute:
    """`self._limit` is the base's client-side total; providers must not reuse it.

    usgs_water is why this exists. It stored its constructor's *server-side*
    per-request cap in `self._limit` long before the base claimed that name, so
    adding `download(limit=)` made a plain `download()` overwrite it with
    `None` — turning a bounded request into an unbounded one, silently, in the
    one backend family the bounded-result work was supposed to protect. A
    provider-side cap belongs in its own attribute (`_request_limit`).
    """

    #: Attribute the base class owns for the client-side total cap.
    BASE_ATTR = "self._limit"

    def _assignments_to_base_attr(self, source: str) -> list[str]:
        """Return the right-hand sides assigned to `self._limit` in `source`."""
        found: list[str] = []
        for node in ast.walk(ast.parse(source)):
            # `AnnAssign` too: `self._limit: int | None = limit` is the same
            # shadowing written with an annotation, and an `Assign`-only walk
            # lets it back in unflagged.
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            for target in targets:
                if ast.unparse(target) == self.BASE_ATTR:
                    found.append(ast.unparse(value))
        return found

    def test_only_a_validated_cap_is_stored_in_the_base_attribute(self):
        """Every write to `self._limit` must be a `check_limit(...)` result.

        A raw `self._limit = limit` is the shape of a provider borrowing the
        name for its own meaning — and it also skips validation.
        """
        offenders: list[str] = []
        root = Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            for value in self._assignments_to_base_attr(
                path.read_text(encoding="utf-8")
            ):
                if "check_limit(" not in value:
                    offenders.append(f"{path.parent.name}: self._limit = {value}")
        assert offenders == [], (
            f"these assign something other than a validated cap to the "
            f"base-owned self._limit; a provider-side cap needs its own "
            f"attribute: {offenders}"
        )

    def test_a_provider_side_cap_still_reaches_its_query(self):
        """The renamed attribute is wired, not merely stored.

        Guards the rename itself: moving fdsn/usgs_water to `_request_limit`
        would be a regression if the query kept reading the old name.
        """
        checks = {
            "libs/providers/hazards/src/earthlens/fdsn/backend.py": (
                "limit=self._request_limit,"
            ),
            "libs/providers/ocean/src/earthlens/usgs_water/backend.py": (
                "limit=self._request_limit,"
            ),
        }
        root = Path(__file__).resolve().parents[3]
        for relative, expected in checks.items():
            source = (root / relative).read_text(encoding="utf-8")
            assert expected in source, (
                f"{relative} no longer passes its provider-side cap to the "
                f"query; the server-side bound is gone"
            )


class TestSourceTextIsNotDoubleEncoded:
    """No source file carries mojibake from a UTF-8/cp1252 round-trip.

    Six backends shipped `â€"` where an em-dash belonged — the byte sequence a
    UTF-8 em-dash becomes when it is decoded as cp1252 and re-encoded. It hid
    well: ruff, mypy and the whole suite are indifferent to string *contents*,
    and a Windows terminal renders the mangled bytes back as `—`, so reading
    the file in a console makes it look correct. These strings are user-facing
    (they are the `aggregate=` refusal messages), so the corruption reaches
    error output and the rendered docs.
    """

    #: `â€"` / `â€"` / `â€¦` — the cp1252 re-encodings of U+2014, U+201D, U+2026.
    MOJIBAKE = "\u00e2\u20ac"

    def test_no_source_file_contains_mojibake(self):
        """A `â€`-prefixed sequence is never intentional in this codebase."""
        root = Path(__file__).resolve().parents[3]
        offenders: list[str] = []
        for pattern in (
            "libs/providers/*/src/earthlens/**/*.py",
            "libs/core/src/earthlens/**/*.py",
        ):
            for path in sorted(root.glob(pattern)):
                if "build" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                if self.MOJIBAKE in text:
                    line = next(
                        index
                        for index, content in enumerate(text.splitlines(), 1)
                        if self.MOJIBAKE in content
                    )
                    offenders.append(f"{path.name}:{line}")
        assert offenders == [], (
            f"double-encoded characters found; these render as 'â€\"' in error "
            f"messages and docs: {offenders}"
        )


class TestArgsEntriesAreOnTheirOwnLine:
    """Two `Args:` entries must never share a physical line.

    A scripted docstring edit that inserts `limit:` after `progress_bar:`
    without a newline produces
    `progress_bar: Show a bar.            limit: Cap on the rows...`.
    Python does not care, the tests do not care, and ruff does not read
    docstring structure — but Google-style parsers key on one entry per line,
    so the second parameter silently vanishes from the rendered documentation.
    That happened to nrel and pvgis in this branch.
    """

    def test_no_two_arg_entries_share_a_line(self):
        """Each `name: description` entry starts its own line."""
        import re

        root = Path(__file__).resolve().parents[3]
        offenders: list[str] = []
        pattern = re.compile(r"\S {2,}[a-z_]+:\s")
        for glob in (
            "libs/providers/*/src/earthlens/**/*.py",
            "libs/core/src/earthlens/**/*.py",
        ):
            for path in sorted(root.glob(glob)):
                if "build" in path.parts:
                    continue
                # Scoped to lines *inside* an `Args:` block. The first version
                # skipped any line containing `{}=()[]` instead, which is most
                # real Args prose (`lat_lim: [lat_min, lat_max] in degrees`) —
                # it would have missed the nrel/pvgis defect it was written for.
                in_args = False
                args_indent = 0
                for index, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    indent = len(line) - len(line.lstrip())
                    if stripped in ("Args:", "Arguments:"):
                        in_args, args_indent = True, indent
                        continue
                    if in_args and indent <= args_indent:
                        in_args = False
                    if not in_args:
                        continue
                    # Doctest lines carry their own `name: value` syntax —
                    # class-body annotations and inline YAML — which is not an
                    # Args entry at all.
                    if stripped.startswith(("#", ">>>", "...")):
                        continue
                    if pattern.search(line):
                        offenders.append(f"{path.name}:{index}")
        assert offenders == [], (
            f"two Args entries share one line, so the second is dropped from "
            f"the rendered docs: {offenders}"
        )


class TestCatalogLoadingUsesOneMechanism:
    """A catalog must not define `_autoload` that its own post-init skips.

    ARC-4 moved 32 catalogs onto the shared `_autoload` hook and deliberately
    left the rest loading inline in their own `model_post_init`. Both are fine.
    What is not fine is a class doing both halves inconsistently: defining
    `_autoload` while overriding `model_post_init` without chaining to the base
    means the hook is never called, and the catalog silently loads by whichever
    path happens to be wired — with the dead one sitting there looking correct.
    """

    def _catalog_classes(self):
        """Yield `(label, ClassDef)` for every `AbstractCatalog` subclass."""
        root = Path(__file__).resolve().parents[2] / "providers"
        for path in sorted(root.rglob("src/earthlens/*/catalog.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if any("AbstractCatalog" in ast.unparse(b) for b in node.bases):
                    yield f"{path.parent.name}.{node.name}", node

    def test_catalog_classes_were_found(self):
        """Guard the guard: the scan must reach the shipped catalogs."""
        found = [label for label, _ in self._catalog_classes()]
        # ~48 backends ship a catalog; a floor of 40 fails if the scan
        # silently stops finding most of them.
        assert len(found) >= 40, f"only found {len(found)}: {found}"

    def test_no_catalog_defines_an_autoload_it_never_calls(self):
        """Defining `_autoload` and skipping `super()` leaves it dead."""
        offenders: list[str] = []
        for label, node in self._catalog_classes():
            body = ast.unparse(node)
            defines_autoload = any(
                isinstance(member, ast.FunctionDef) and member.name == "_autoload"
                for member in node.body
            )
            overrides = [
                member
                for member in node.body
                if isinstance(member, ast.FunctionDef)
                and member.name == "model_post_init"
            ]
            if not defines_autoload or not overrides:
                continue
            if "super().model_post_init" not in body:
                offenders.append(label)
        assert offenders == [], (
            f"these define _autoload but override model_post_init without "
            f"calling super(), so the hook never runs: {offenders}"
        )


class TestLimitIsReachableThroughTheFacade:
    """A backend that takes a `limit` must accept it on `download`.

    The facade forwards `**kwargs` to `backend.download`, so a backend whose
    cap lives only on `__init__` answers `EarthLens(...).download(limit=100)`
    with `TypeError: got an unexpected keyword argument`. openaq and fdsn were
    in that shape while thirteen other backends took the keyword happily —
    the same argument name working or failing depending on which backend was
    bound is the kind of inconsistency a user cannot predict.
    """

    def test_every_backend_with_a_cap_takes_it_on_download(self):
        """No backend accepts `limit` only at construction."""
        root = Path(__file__).resolve().parents[2] / "providers"
        offenders: list[str] = []
        for path in sorted(root.rglob("src/earthlens/*/backend.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            takes = {"__init__": False, "download": False}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in takes:
                    names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                    takes[node.name] = takes[node.name] or "limit" in names
            if takes["__init__"] and not takes["download"]:
                offenders.append(path.parent.name)
        assert offenders == [], (
            f"these take a limit= at construction but not on download, so "
            f"EarthLens(...).download(limit=...) raises TypeError for them: "
            f"{offenders}"
        )


class TestBoundedResultsDocMatchesTheCode:
    """The `limit=` reference page must list every backend that takes one.

    Documentation that enumerates backends goes stale the moment one is added,
    and a table that silently omits a backend is worse than no table — a reader
    concludes the cap is unavailable there. This pins the list to the code that
    it describes.
    """

    def test_every_capped_backend_appears_in_the_table(self):
        """A backend with `download(limit=)` is named on the page."""
        root = Path(__file__).resolve().parents[3]
        page = root / "docs" / "reference" / "base" / "bounded-results.md"
        assert page.exists(), f"the bounded-results reference page is missing: {page}"
        text = page.read_text(encoding="utf-8")

        missing: list[str] = []
        provider_root = root / "libs" / "providers"
        for path in sorted(provider_root.rglob("src/earthlens/*/backend.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.FunctionDef) and node.name == "download"):
                    continue
                names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                if "limit" in names and path.parent.name not in text:
                    missing.append(path.parent.name)
                break
        assert missing == [], (
            f"these backends accept limit= but are not named on "
            f"docs/reference/base/bounded-results.md, so a reader would think "
            f"the cap is unavailable for them: {missing}"
        )


class TestCliIsBackendAgnostic:
    """#863: core's CLI must not name a backend once tooling migration completes.

    Core defines the catalog-tooling mechanism but must own no per-backend
    handler: each provider publishes its refresh / probe / validate handlers
    through the `earthlens.cli` entry-point group, and core's dispatch dicts are
    projected from that discovery (`earthlens._cli_tooling.dispatch_table`).

    The migration is incremental, so the two allow-lists below name the backends
    whose tooling still lives in core's `cli/` — as literal dispatch-dict keys
    (`PENDING`) or as `earthlens.<backend>` imports (`PENDING_IMPORTS`). Each is
    an exact set: migrating a provider deletes it from both, and adding a new
    hard-coded backend fails the test. When both reach `frozenset()`, the exact
    assertions become "core names no backend", which is the closing condition
    for the issue. The `query.DEFAULT_PROVIDER_PRIORITY` ordering tuple is a
    deliberate exception (a UX precedence hint, env-overridable, coupling to no
    provider code), so it is not scanned — only dict keys and imports are.
    """

    #: Backends still named as dispatch-dict keys in core's CLI (shrinks to
    #: empty as #863 lands provider by provider).
    PENDING: frozenset[str] = frozenset()

    #: Backends core's CLI still imports directly (`from earthlens.<backend>`),
    #: also shrinking to empty.
    PENDING_IMPORTS: frozenset[str] = frozenset()

    def _cli_sources(self):
        """Yield every core CLI source file, skipping build artefacts.

        Covers the `cli/` package plus `earthlens/_cli_tooling.py` — the
        discovery mechanism sits one level above `cli/` but is exactly the
        module that defines provider dispatch, so a hard-coded fallback added
        there must be caught too.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "earthlens"
        paths = [*sorted((src / "cli").rglob("*.py")), src / "_cli_tooling.py"]
        for path in paths:
            if "build" not in path.parts:
                yield path

    def _provider_ids(self) -> set[str]:
        """The canonical provider ids (the `earthlens.<id>` package names)."""
        from earthlens.cli.adapter import list_backends

        return {info.provider for info in list_backends()}

    def _provider_keys(self) -> set[str]:
        """Every facade key a hard-coded branch might name — canonical + aliases.

        Keying the dict-key / comparison scans off this (rather than the
        canonical ids only) closes the alias hole: a branch written against an
        alias (`"chirps"` for chc, `"amazon-s3"` for s3) is still recognised as
        naming a backend.
        """
        from earthlens.cli.adapter import known_provider_keys

        return set(known_provider_keys())

    def test_dispatch_dicts_name_exactly_the_pending_backends(self):
        """Every provider-id dict key in core's CLI is an unmigrated backend."""
        provider_keys = self._provider_keys()
        named: set[str] = set()
        for path in self._cli_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Dict):
                    continue
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in provider_keys:
                        named.add(key.value)
        assert named == self.PENDING, (
            "core CLI dispatch dicts name a set of backends other than the "
            f"pending allow-list; extra={sorted(named - self.PENDING)}, "
            f"missing={sorted(self.PENDING - named)}. Migrate the backend to "
            "earthlens.<backend>.cli and update PENDING (empty = #863 closed)."
        )

    def test_no_provider_id_comparisons_in_core_cli(self):
        """No core CLI branch compares against a backend id / alias string.

        Catches the coupling forms the dict-key scan cannot see: a
        `provider == "gee"` equality, an `!=`, or a membership test against a
        set/list/tuple literal (`provider in {"gee", "ecmwf"}`) — the shapes a
        reintroduced `if`/`elif` dispatch would take. Docstring examples are
        exempt automatically: they parse as a single string constant, never as
        `ast.Compare` nodes.
        """
        provider_keys = self._provider_keys()
        offenders: set[str] = set()
        for path in self._cli_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Compare):
                    continue
                operands = [node.left, *node.comparators]
                literals: list[ast.expr] = []
                for operand in operands:
                    if isinstance(operand, ast.Set | ast.List | ast.Tuple):
                        literals.extend(operand.elts)
                    else:
                        literals.append(operand)
                for literal in literals:
                    if (
                        isinstance(literal, ast.Constant)
                        and literal.value in provider_keys
                    ):
                        offenders.add(literal.value)
        allowed = self.PENDING | self.PENDING_IMPORTS
        assert offenders <= allowed, (
            "core CLI compares against backend id / alias string(s) "
            f"{sorted(offenders - allowed)} — a hard-coded provider branch. "
            "Dispatch on a role via earthlens._cli_tooling instead."
        )

    def test_no_provider_id_dispatch_shapes_in_core_cli(self):
        """No core CLI branch dispatches on a backend id via subscript/call/match.

        Complements the dict-key and comparison scans by catching the remaining
        shapes a hand-rolled dispatch could take: a constant subscript against a
        name-bound registry (`REGISTRY["gee"]`), a backend id passed as a call
        argument (`table.get("gee")`, `f("ecmwf", ...)`), or a structural
        `match` / `case "gee":`. Docstring examples are exempt automatically
        (they parse as a single string constant, not as these nodes).
        """
        provider_keys = self._provider_keys()
        offenders: set[str] = set()

        def flag(node: ast.expr | None) -> None:
            if isinstance(node, ast.Constant) and node.value in provider_keys:
                offenders.add(node.value)

        for path in self._cli_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Subscript):
                    flag(node.slice)
                elif isinstance(node, ast.Call):
                    for arg in node.args:
                        flag(arg)
                    for kw in node.keywords:
                        flag(kw.value)
                elif isinstance(node, ast.MatchValue):
                    flag(node.value)
        allowed = self.PENDING | self.PENDING_IMPORTS
        assert offenders <= allowed, (
            "core CLI dispatches on backend id / alias string(s) "
            f"{sorted(offenders - allowed)} via a subscript / call arg / match "
            "case — a hard-coded provider branch. Use a role via "
            "earthlens._cli_tooling instead."
        )

    def test_provider_imports_are_exactly_the_pending_ones(self):
        """Every `earthlens.<backend>` import in core's CLI is unmigrated.

        Scans both `from earthlens.<backend> import …` (`ast.ImportFrom`) and
        the plain `import earthlens.<backend>[.…]` form (`ast.Import`).
        """
        provider_ids = self._provider_ids()
        imported: set[str] = set()
        for path in self._cli_sources():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    parts = module.split(".")
                    if len(parts) > 1 and parts[0] == "earthlens":
                        if parts[1] in provider_ids:
                            imported.add(parts[1])
        assert imported == self.PENDING_IMPORTS, (
            "core CLI imports a set of provider packages other than the pending "
            f"allow-list; extra={sorted(imported - self.PENDING_IMPORTS)}, "
            f"missing={sorted(self.PENDING_IMPORTS - imported)}. Move the handler "
            "into the provider and update PENDING_IMPORTS (empty = #863 closed)."
        )


class TestSingleSecretAuthAdoption:
    """#833: the single-secret credential ceremony lives once, in the base.

    Six backends authenticate with a single *mandatory* API key / token and each
    used to carry the same ceremony by hand: resolve an explicit argument, fall
    back to an environment variable, raise `AuthenticationError` when neither is
    present, then memoise. That ceremony now lives in
    `earthlens.base.SingleSecretAuth`; each backend supplies only its `ENV_VARS`
    / `PROVIDER` and the `_explicit_credential` / `_connect` hooks. This guard
    keeps them on the shared base and off a re-grown `os.environ` fallback.

    The exclusions are deliberate, not omissions:

    * `usgs_water` has the same single-secret *shape* but its token is optional —
      a missing one falls back to anonymous access rather than raising, so it does
      not fit the always-raise `SingleSecretAuth` contract and keeps its own
      `configure`.
    * the multi-field / OAuth backends (`nrel`, `cmems`, `earthdata`, `emdat`,
      `eumetsat`, `jaxa`, `openeo`, `sentinel_hub`, `gee`) resolve more than one
      field and extend `AbstractAuth` directly by design.
    """

    #: Backends whose single mandatory secret must resolve through the base.
    SINGLE_SECRET = frozenset(
        {"airnow", "firms", "iucn", "openaq", "risk_indicators", "wdpa"}
    )

    def _auth_path(self, backend: str) -> Path:
        """Return the one `auth.py` for `backend`, asserting it is unique."""
        matches = sorted(
            _ROOT.glob(f"libs/providers/*/src/earthlens/{backend}/auth.py")
        )
        assert len(matches) == 1, f"expected one auth.py for {backend}, got {matches}"
        return matches[0]

    def test_single_secret_backends_were_found(self):
        """Guard the guard: every named backend resolves to exactly one auth.py."""
        for backend in sorted(self.SINGLE_SECRET):
            assert self._auth_path(backend).exists()

    def test_single_secret_backends_extend_the_shared_base(self):
        """Each single-secret `Auth` subclasses `SingleSecretAuth`, not `AbstractAuth`."""
        offenders = []
        for backend in sorted(self.SINGLE_SECRET):
            tree = ast.parse(self._auth_path(backend).read_text(encoding="utf-8"))
            bases = {
                ast.unparse(base)
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
                for base in node.bases
            }
            if not any(base.startswith("SingleSecretAuth[") for base in bases):
                offenders.append(backend)
        assert offenders == [], (
            f"these single-secret backends do not extend SingleSecretAuth, so "
            f"they carry their own credential ceremony: {offenders}"
        )

    def test_single_secret_backends_do_not_reimplement_the_env_fallback(self):
        """None of them still reads `os.environ` — the fallback lives in the base.

        The explicit-or-`os.environ.get` chain that raises when the secret is
        absent now lives once in `SingleSecretAuth._resolve_credential`. A read of
        `os.environ` in one of these modules means a hand-rolled copy has crept
        back in.
        """
        offenders = []
        for backend in sorted(self.SINGLE_SECRET):
            source = self._auth_path(backend).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "environ"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                ):
                    offenders.append(backend)
                    break
        assert offenders == [], (
            f"these single-secret backends still read os.environ instead of "
            f"delegating the fallback to SingleSecretAuth: {offenders}"
        )


class TestNoDirectGdal:
    """GIS I/O goes through pyramids; `osgeo` has exactly one sanctioned site."""

    def test_no_unsanctioned_gdal_import(self):
        """Every earthlens source is free of GDAL beyond the declared allowance."""
        offenders = {
            str(path.relative_to(_ROOT)).replace("\\", "/"): _gis_imports(path)
            for path in _earthlens_sources()
        }
        offenders = {name: hits for name, hits in offenders.items() if hits}
        unsanctioned = {
            name: hits for name, hits in offenders.items() if name not in _GDAL_ALLOWED
        }
        assert unsanctioned == {}, (
            "GIS I/O belongs to pyramids -- use Dataset / NetCDF / FeatureCollection. "
            "If pyramids genuinely cannot do it, file an issue there and add the site "
            f"to _GDAL_ALLOWED with the issue number: {unsanctioned}"
        )

    def test_the_allowance_is_still_used(self):
        """A stale allowance is a lie about the codebase, so it must still bite."""
        live = {
            str(path.relative_to(_ROOT)).replace("\\", "/")
            for path in _earthlens_sources()
            if _gis_imports(path)
        }
        assert _GDAL_ALLOWED <= live, (
            "_GDAL_ALLOWED names a file that no longer imports GDAL; drop the entry: "
            f"{sorted(_GDAL_ALLOWED - live)}"
        )

    def test_the_sanctioned_site_imports_pyramids_first(self):
        """`osgeo` only resolves once pyramids has vendored it onto the path."""
        # Asserted per enclosing function, not per file: the rule is that
        # pyramids is imported inside the function that reaches for GDAL, and a
        # module-scope import would pass a "somewhere earlier in the file"
        # check while breaking it. Matching AST nodes rather than literal text
        # also keeps this reporting a failure, not a ValueError, if the site
        # ever switches to `import osgeo.gdal`.
        for name in sorted(_GDAL_ALLOWED):
            path = _ROOT / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sites = [
                (func, node)
                for func in ast.walk(tree)
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(func)
                if _is_banned_gis_import(node)
            ]
            assert sites, (
                f"{name} is allowed to import GDAL but no function does; the "
                "import must live in the function that needs it, not at module scope"
            )
            # Everything `sites` found is inside a function; anything else in the
            # file is at module (or class) scope, which is the arrangement the
            # rule forbids and which the per-function loop below cannot see.
            in_a_function = {id(node) for _, node in sites}
            stray = sorted(
                node.lineno
                for node in ast.walk(tree)
                if _is_banned_gis_import(node) and id(node) not in in_a_function
            )
            assert not stray, (
                f"{name} imports GDAL at module scope (line {stray}); it belongs "
                "inside the function that needs it, so the cost is paid only on "
                "that path and pyramids is guaranteed to have run first"
            )
            for func, node in sites:
                first = [
                    other.lineno
                    for other in ast.walk(func)
                    if _imports_pyramids(other) and other.lineno < node.lineno
                ]
                assert first, (
                    f"{name}:{node.lineno} imports GDAL inside {func.name}() without "
                    "importing pyramids first in that same function, which raises "
                    "ModuleNotFoundError outside an already-loaded process"
                )


#: Every spelling that reaches GDAL, paired with the id it reports under. The
#: guard is only worth its name if it catches all of them, and nothing here is
#: hypothetical -- the bare `import_module` form slipped through until it was
#: probed, because the matcher keyed off the AST node shape instead of the
#: callee's name.
_GDAL_SPELLINGS = [
    ("plain", "import osgeo\n"),
    ("dotted", "import osgeo.gdal\n"),
    ("aliased", "import osgeo.gdal as g\n"),
    ("from", "from osgeo import gdal\n"),
    ("from-dotted", "from osgeo.gdal import Translate\n"),
    ("sibling-ogr", "from ogr import Open\n"),
    ("sibling-osr", "import osr\n"),
    (
        "dynamic-dotted",
        'import importlib\nimportlib.import_module("osgeo")\n',
    ),
    (
        "dynamic-bare",
        'from importlib import import_module\nimport_module("osgeo")\n',
    ),
    ("dynamic-dunder", '__import__("osgeo")\n'),
]

#: Sources that must NOT trip the guard, so it stays usable. The relative forms
#: matter: `from .osgeo import gdal` names a sibling module inside earthlens, so
#: it is not GDAL and flagging it would be a false positive.
_INNOCENT_SOURCES = [
    ("relative-module", "from .osgeo import gdal\n"),
    ("relative-package", "from . import osgeo\n"),
    ("pyramids", "import pyramids\n"),
    ("pyramids-from", "from pyramids.dataset import Dataset\n"),
    (
        "unrelated-dynamic",
        'import importlib\nimportlib.import_module("json")\n',
    ),
    ("name-merely-contains-osgeo", "import osgeohelper\n"),
]


class TestGdalGuardDetection:
    """The GDAL guard is only as good as what `_gis_imports` can see."""

    @pytest.mark.parametrize(
        "source",
        [src for _, src in _GDAL_SPELLINGS],
        ids=[i for i, _ in _GDAL_SPELLINGS],
    )
    def test_every_gdal_spelling_is_caught(self, source, tmp_path):
        """Each way of reaching GDAL is detected, static or dynamic."""
        probe = tmp_path / "probe.py"
        probe.write_text(source, encoding="utf-8")
        assert _gis_imports(probe), f"guard missed this import:\n{source}"

    @pytest.mark.parametrize(
        "source",
        [src for _, src in _INNOCENT_SOURCES],
        ids=[i for i, _ in _INNOCENT_SOURCES],
    )
    def test_innocent_sources_are_left_alone(self, source, tmp_path):
        """A guard that fires on ordinary imports would be turned off."""
        probe = tmp_path / "probe.py"
        probe.write_text(source, encoding="utf-8")
        assert _gis_imports(probe) == [], f"guard false-positived on:\n{source}"

    def test_reports_the_line_it_found(self, tmp_path):
        """The offender's line number is reported so the failure is actionable."""
        probe = tmp_path / "probe.py"
        probe.write_text("x = 1\n" + "from osgeo import gdal\n", encoding="utf-8")
        assert _gis_imports(probe) == [(2, "from osgeo")]

    def test_is_banned_gis_import_matches_only_import_nodes(self):
        """The node predicate accepts GDAL imports and rejects anything else."""
        banned = ast.parse("from osgeo import gdal").body[0]
        innocent = ast.parse("import pyramids").body[0]
        call = ast.parse("f()").body[0]
        assert _is_banned_gis_import(banned)
        assert not _is_banned_gis_import(innocent)
        assert not _is_banned_gis_import(call)

    def test_imports_pyramids_matches_every_vendoring_form(self):
        """Any import that loads the pyramids package counts, from-imports included."""
        vendoring = [
            "import pyramids",
            "import pyramids.dataset",
            "from pyramids import dataset",
            "from pyramids.dataset import Dataset",
        ]
        for source in vendoring:
            node = ast.parse(source).body[0]
            assert _imports_pyramids(node), f"{source!r} does vendor osgeo"
        assert not _imports_pyramids(ast.parse("from json import loads").body[0])
