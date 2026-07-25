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
