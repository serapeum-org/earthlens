"""Facade-routing tests for the Tropycal backend (`EarthLens` -> `TropicalCyclone`).

The headline test is the aggregate-rejection path: Tropycal is a `vector`
backend, so this exercises the facade's `OUTPUT_KIND`-driven `aggregate=`
guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.earthlens import EarthLens
from geopandas import GeoDataFrame

import earthlens.tropycal

from .conftest import _FakeState


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the Tropycal backend."""
    params: dict[str, object] = dict(
        variables=["north_atlantic"],
        data_source="tropycal",
        start="2005-08-01",
        end="2005-09-01",
        lat_lim=[18.0, 31.0],
        lon_lim=[-98.0, -80.0],
        source="hurdat",
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.tropycal
class TestFacadeRouting:
    """The `tropycal` key resolves to and constructs the backend."""

    def test_key_registered(self):
        """`tropycal` is among the registered data sources."""
        assert "tropycal" in EarthLens.DataSources

    def test_key_resolves_to_class(self):
        """The `tropycal` key resolves to `earthlens.tropycal.TropicalCyclone`."""
        assert EarthLens.DataSources["tropycal"] is earthlens.tropycal.TropicalCyclone

    def test_facade_builds_backend(self, tmp_path: Path):
        """The facade binds a TropicalCyclone instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.tropycal.TropicalCyclone)

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """The geometry / source kwargs ride through `**backend_kwargs`."""
        facade = _make_facade(tmp_path, geometry="track", source="ibtracs")
        assert facade.datasource._geometry == "track"
        assert facade.datasource._source == "ibtracs"


@pytest.mark.tropycal
class TestFacadeAggregateRejection:
    """The headline guard: a vector backend rejects `aggregate=`."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        facade = _make_facade(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "aggregate= is not supported" in str(exc.value), (
            f"rejection message should name the guard, got: {exc.value}"
        )

    def test_aggregate_none_is_allowed(self, tmp_path: Path, fake_tropycal: _FakeState):
        """`aggregate=None` is fine and a normal download runs."""
        facade = _make_facade(tmp_path)
        fc = facade.download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)


@pytest.mark.tropycal
class TestFacadeDownload:
    """The facade returns the backend's FeatureCollection."""

    def test_download_returns_feature_collection(
        self, tmp_path: Path, fake_tropycal: _FakeState
    ):
        """A facade download returns the track FeatureCollection."""
        fc = _make_facade(tmp_path).download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 3, f"expected 3 fixes, got {len(fc)}"
