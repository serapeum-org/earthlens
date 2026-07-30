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

import pandas as pd
import pytest

from earthlens.base import AbstractDataSource, OutputKind, TemporalExtent
from earthlens.chc import CHIRPS
from earthlens.earthlens import EarthLens
from earthlens.ecmwf import ECMWF
from earthlens.s3 import S3


class _GuardBackend(AbstractDataSource):
    """Minimal real backend whose `OUTPUT_KIND` a test can set per instance.

    A real subclass, not a mock: the `aggregate=` guard lives in the `download`
    wrapper that `__init_subclass__` installs, so a mock would not be guarded at
    all.
    """

    REQUIRES_TIME_WINDOW = False
    SUPPORTS_AGGREGATE = True

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution="all",
            dates=pd.DatetimeIndex([]),
        )

    def download(self, progress_bar: bool = True, aggregate=None, **kwargs):
        """Record the call so a test can assert what reached the backend."""
        self.calls.append({"progress_bar": progress_bar, "aggregate": aggregate})
        return []


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
            f"{backend_cls.__name__} drifted from raster: {backend_cls.OUTPUT_KIND!r}"
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
    """`download(aggregate=...)` is gated by the backend, reached via the facade.

    The gate used to live in the facade and test against a `MagicMock`
    datasource. It now lives in the `download` wrapper every
    `AbstractDataSource` subclass gets, which is why these use a real subclass:
    the wrapper also guards a direct `backend.download(aggregate=...)`, which a
    facade-only check never did, and the facade is contracted to hold a real
    backend anyway.
    """

    @pytest.fixture
    def fake_backend(self):
        """A real `AbstractDataSource` subclass standing in for a backend."""
        backend = _GuardBackend(
            start=None,
            end=None,
            variables=["x"],
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path="",
        )
        backend.calls = []
        return backend

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
        assert fake_backend.calls[-1]["aggregate"] is cfg, (
            f"aggregate not forwarded for OUTPUT_KIND={kind!r}: {fake_backend.calls!r}"
        )

    @pytest.mark.parametrize("kind", ["vector", "tabular"])
    def test_aggregate_rejected_for_disallowed_kinds(self, facade, fake_backend, kind):
        """A non-gridded instance refuses `aggregate=`, even when the class supports it."""
        fake_backend.OUTPUT_KIND = kind
        cfg = object()
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            facade.download(progress_bar=False, aggregate=cfg)
        assert fake_backend.calls == [], "the backend body must not run"

    def test_rejected_on_a_direct_call_too(self, fake_backend):
        """The guard is the backend's, so bypassing the facade does not bypass it."""
        fake_backend.OUTPUT_KIND = "tabular"
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            fake_backend.download(aggregate=object())
        assert fake_backend.calls == []

    def test_class_that_does_not_support_it_refuses_even_for_raster(self, fake_backend):
        """`SUPPORTS_AGGREGATE = False` refuses regardless of a gridded kind."""
        fake_backend.OUTPUT_KIND = "raster"
        fake_backend.SUPPORTS_AGGREGATE = False
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            fake_backend.download(aggregate=object())

    def test_aggregate_none_bypasses_guard_for_any_kind(self, facade, fake_backend):
        """`aggregate=None` (default) never trips the guard, even for vector backends."""
        fake_backend.OUTPUT_KIND = "vector"
        facade.download(progress_bar=False)
        assert fake_backend.calls[-1]["aggregate"] is None

    def test_missing_output_kind_attr_defaults_to_raster(self, facade):
        """A datasource with no `OUTPUT_KIND` is treated as raster (back-compat).

        Such an object is not an `AbstractDataSource`, so it has no `download`
        wrapper and therefore no guard — the facade forwards and the object
        decides for itself.
        """
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
        with pytest.raises(NotImplementedError) as exc_info:
            facade.download(progress_bar=False, aggregate=object())
        msg = str(exc_info.value)
        assert "tabular" in msg, f"kind missing from message: {msg!r}"
        assert "OUTPUT_KIND=" in msg, f"OUTPUT_KIND label missing: {msg!r}"


class TestPositionalAggregateReachesTheGate:
    """A positionally-passed `aggregate` is refused like the keyword form.

    `aggregate` is the second positional parameter on the backends that declare
    it, so a gate reading only `**kwargs` let `download(False, config)` through.
    This file tests the `OUTPUT_KIND` gate and had no positional case, which is
    why it did not catch that.
    """

    def test_positional_and_keyword_forms_agree(self, tmp_path):
        """Both call shapes raise the same refusal."""
        backend = _GuardBackend(
            start=None,
            end=None,
            variables=["x"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
        )
        backend.calls = []
        backend.OUTPUT_KIND = "vector"
        with pytest.raises(NotImplementedError) as positional:
            backend.download(False, object())
        with pytest.raises(NotImplementedError) as keyword:
            backend.download(aggregate=object())
        assert str(positional.value) == str(keyword.value)
