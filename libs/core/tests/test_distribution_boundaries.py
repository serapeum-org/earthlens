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
            if "SUPPORTS_AGGREGATE = True" not in source:
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
            if not uses_it:
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
    BOUNDING_HELPERS = ("_take_limited", "_fetch_limited", "iter_download")

    def _pushes_limit_to_the_service(self, source: str) -> bool:
        """Whether the cap is handed to the provider's own query.

        The strongest bound available: the service never sends the rows past
        the cap, so there is nothing to trim client-side and no helper is
        needed. fdsn does this — `limit=self._limit` goes into the FDSN
        `get_events` call. Detected structurally (a `limit=self._limit`
        keyword on a call that is not one of the bounding helpers) so a
        backend cannot claim it with a comment.
        """
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
                    and ast.unparse(keyword.value) == "self._limit"
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
        assert len(found) >= 8, f"only found {found}"

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
