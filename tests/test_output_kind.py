"""Tests for the C1 `OUTPUT_KIND` extension point + facade aggregate guard.

Covers:
- `OutputKind` type alias (the 4 allowed literal values).
- `AbstractDataSource.OUTPUT_KIND` class attribute (default `"raster"`).
- Inheritance of the default by the existing 4 backends (CHIRPS, S3,
  ECMWF, GEE).
- `EarthLens.download(aggregate=...)` rejects vector / tabular backends
  with `NotImplementedError` and forwards to raster / mixed.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import MagicMock

import pytest

from earthlens.base import AbstractDataSource, OutputKind
from earthlens.chc import CHIRPS
from earthlens.earthlens import EarthLens
from earthlens.ecmwf import ECMWF
from earthlens.s3 import S3


@pytest.mark.unit
class TestOutputKindLiteral:
    """The `OutputKind` literal exposes exactly the five documented values."""

    def test_outputkind_args_are_the_four_documented_strings(self):
        """`get_args(OutputKind)` yields the canonical 4-tuple."""
        assert get_args(OutputKind) == (
            "raster",
            "vector",
            "tabular",
            "mixed",
        ), f"OutputKind args drifted: {get_args(OutputKind)!r}"


@pytest.mark.unit
class TestAbstractDataSourceOutputKindDefault:
    """The base class declares `OUTPUT_KIND = 'raster'`; every existing backend inherits."""

    def test_base_default_is_raster(self):
        """`AbstractDataSource.OUTPUT_KIND` defaults to the raster literal."""
        assert AbstractDataSource.OUTPUT_KIND == "raster"

    @pytest.mark.parametrize("backend_cls", [CHIRPS, ECMWF])
    def test_existing_backends_inherit_raster(self, backend_cls):
        """CHIRPS / ECMWF inherit the raster default unchanged (C1 back-compat)."""
        assert backend_cls.OUTPUT_KIND == "raster", (
            f"{backend_cls.__name__} drifted from raster: "
            f"{backend_cls.OUTPUT_KIND!r}"
        )

    def test_s3_backend_is_mixed(self):
        """The S3 backend is multi-dataset (NetCDF + COG), so it declares mixed."""
        assert S3.OUTPUT_KIND == "mixed", (
            f"S3 should be mixed (multi-dataset), got {S3.OUTPUT_KIND!r}"
        )

    def test_gee_backend_inherits_raster(self):
        """GEE backend inherits raster (separate test — it may be installed conditionally)."""
        from earthlens.gee import GEE

        assert GEE.OUTPUT_KIND == "raster", (
            f"GEE drifted from raster: {GEE.OUTPUT_KIND!r}"
        )


@pytest.mark.unit
class TestEarthLensAggregateGuard:
    """Facade `download(aggregate=...)` gates by `datasource.OUTPUT_KIND`."""

    @pytest.fixture
    def fake_backend(self):
        """A MagicMock that stands in for a constructed backend instance."""
        return MagicMock(name="fake_backend")

    @pytest.fixture
    def facade(self, fake_backend):
        """An `EarthLens` whose `.datasource` is the fake backend."""
        el = EarthLens.__new__(EarthLens)
        el.datasource = fake_backend
        return el

    @pytest.mark.parametrize("kind", ["raster", "mixed"])
    def test_aggregate_forwarded_for_allowed_kinds(self, facade, fake_backend, kind):
        """`aggregate=cfg` is forwarded as a backend kwarg for raster/mixed."""
        fake_backend.OUTPUT_KIND = kind
        cfg = object()
        facade.download(progress_bar=False, aggregate=cfg)
        _, kwargs = fake_backend.download.call_args
        assert kwargs.get("aggregate") is cfg, (
            f"aggregate not forwarded for OUTPUT_KIND={kind!r}: kwargs={kwargs!r}"
        )

    @pytest.mark.parametrize("kind", ["vector", "tabular"])
    def test_aggregate_rejected_for_disallowed_kinds(
        self, facade, fake_backend, kind
    ):
        """`aggregate=cfg` raises `NotImplementedError` for vector / tabular."""
        fake_backend.OUTPUT_KIND = kind
        cfg = object()
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            facade.download(progress_bar=False, aggregate=cfg)
        fake_backend.download.assert_not_called()

    def test_aggregate_none_bypasses_guard_for_any_kind(self, facade, fake_backend):
        """`aggregate=None` (default) never trips the guard, even for vector backends."""
        fake_backend.OUTPUT_KIND = "vector"
        facade.download(progress_bar=False)
        _, kwargs = fake_backend.download.call_args
        assert "aggregate" not in kwargs, (
            f"aggregate should be omitted when None: kwargs={kwargs!r}"
        )

    def test_missing_output_kind_attr_defaults_to_raster(self, facade, fake_backend):
        """A backend with no `OUTPUT_KIND` attribute is treated as raster (back-compat)."""
        # `delattr` removes the auto-mock attr so getattr falls back to default
        if hasattr(fake_backend, "OUTPUT_KIND"):
            delattr(fake_backend, "OUTPUT_KIND")
        # Force MagicMock to not auto-generate it on access — use a real obj
        sentinel = type("LegacyBackend", (), {})()
        sentinel.download = MagicMock()
        facade.datasource = sentinel
        cfg = object()
        facade.download(progress_bar=False, aggregate=cfg)
        _, kwargs = sentinel.download.call_args
        assert kwargs.get("aggregate") is cfg, (
            f"missing OUTPUT_KIND should default to raster (forward aggregate); "
            f"kwargs={kwargs!r}"
        )

    def test_error_message_names_backend_class_and_kind(self, facade, fake_backend):
        """The `NotImplementedError` message includes the backend class name and kind."""
        fake_backend.OUTPUT_KIND = "tabular"
        fake_backend.__class__.__name__ = "FakeTabularBackend"
        with pytest.raises(NotImplementedError) as exc_info:
            facade.download(progress_bar=False, aggregate=object())
        msg = str(exc_info.value)
        assert "tabular" in msg, f"kind missing from message: {msg!r}"
        assert "OUTPUT_KIND=" in msg, f"OUTPUT_KIND label missing: {msg!r}"
