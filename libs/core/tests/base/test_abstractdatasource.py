"""Unit tests for `earthlens.base.abstractdatasource` module-level helpers."""

from __future__ import annotations

import pytest

from earthlens.base.abstractdatasource import native_parameters


@pytest.mark.unit
class TestNativeParameters:
    """Tests for `native_parameters`."""

    def test_returns_declared_names_without_the_synthesised_ones(self):
        """A wrapper-provided `aoi` is excluded; the backend's own names remain."""
        from earthlens.chc import CHIRPS

        declared = native_parameters(CHIRPS)
        assert "lat_lim" in declared, (
            f"lat_lim should be declared, got {sorted(declared)}"
        )
        assert "aoi" not in declared, "the wrapper's aoi must not read as native"

    def test_keeps_a_backend_that_declares_aoi_itself(self):
        """A backend with a real `aoi=` parameter still reports it.

        This is the distinction the facade routes on: a native `aoi=` is passed
        through verbatim, a wrapper-provided one is resolved to lat/lon bounds.
        """
        from earthlens.core import EarthLens

        assert "aoi" in native_parameters(EarthLens.DataSources["worldpop"])

    def test_returns_empty_when_the_class_has_no_init(self):
        """A class whose `__init__` resolves to `None` yields no names."""

        class _NoInit:
            __init__ = None

        assert native_parameters(_NoInit) == frozenset(), (
            "a missing __init__ should degrade to an empty set, not raise"
        )

    def test_returns_empty_when_the_signature_cannot_be_read(self):
        """An uninspectable `__init__` yields no names rather than raising.

        `inspect.signature` raises `TypeError` for a non-callable, which a
        caller asking "what does this declare?" should not have to handle.
        """

        class _BadInit:
            __init__ = 42

        assert native_parameters(_BadInit) == frozenset(), (
            "an uninspectable __init__ should degrade to an empty set"
        )
