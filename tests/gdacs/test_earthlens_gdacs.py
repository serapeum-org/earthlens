"""Facade-routing tests for the GDACS backend (`EarthLens` -> `GDACS`).

The headline test is the aggregate-rejection path: GDACS is a `vector`
backend, so this exercises the facade's `OUTPUT_KIND`-driven
`aggregate=` guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from geopandas import GeoDataFrame

import earthlens.gdacs
from earthlens.earthlens import EarthLens

from .conftest import _FakeGdacs


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the GDACS backend."""
    params: dict[str, object] = dict(
        variables=["EQ"],
        data_source="gdacs",
        start="2026-05-01",
        end="2026-05-21",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.gdacs
class TestFacadeRouting:
    """The `gdacs` key resolves to and constructs the GDACS backend."""

    def test_key_registered(self):
        """`gdacs` is among the registered data sources."""
        assert "gdacs" in EarthLens.DataSources

    def test_key_resolves_to_gdacs_class(self):
        """The `gdacs` key resolves to `earthlens.gdacs.GDACS`."""
        assert EarthLens.DataSources["gdacs"] is earthlens.gdacs.GDACS

    def test_facade_builds_gdacs_backend(self, tmp_path: Path):
        """The facade binds a GDACS instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.gdacs.GDACS)

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """The alert_level kwarg rides through `**backend_kwargs`."""
        facade = _make_facade(tmp_path, alert_level=["Red"])
        assert facade.datasource._alert_levels == ["Red"]


@pytest.mark.gdacs
class TestFacadeAggregateRejection:
    """The headline guard: a vector backend rejects `aggregate=`."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        facade = _make_facade(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "vector" in str(
            exc.value
        ), f"rejection message should mention 'vector', got: {exc.value}"

    def test_aggregate_none_is_allowed(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """`aggregate=None` is fine and a normal download runs."""
        facade = _make_facade(tmp_path)
        fc = facade.download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)


@pytest.mark.gdacs
class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(
        self, tmp_path: Path, fake_gdacs: _FakeGdacs
    ):
        """A facade download returns the alert FeatureCollection."""
        fc = _make_facade(tmp_path).download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1, f"expected 1 alert, got {len(fc)}"
