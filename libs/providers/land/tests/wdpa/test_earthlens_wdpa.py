"""Facade-routing tests for the WDPA backend (`EarthLens` -> `WDPA`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.earthlens import EarthLens
from geopandas import GeoDataFrame

import earthlens.wdpa


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the WDPA backend."""
    params: dict[str, object] = dict(
        variables=["KEN"],
        data_source="wdpa",
        start="2024-01-01",
        end="2024-12-31",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        token="test-token",
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.wdpa
class TestFacadeRouting:
    """The `wdpa` key (and `protected-planet` alias) resolves to the backend."""

    def test_key_registered(self):
        """`wdpa` is among the registered data sources."""
        assert "wdpa" in EarthLens.DataSources

    def test_key_resolves_to_wdpa_class(self):
        """The `wdpa` key resolves to `earthlens.wdpa.WDPA`."""
        assert EarthLens.DataSources["wdpa"] is earthlens.wdpa.WDPA

    def test_alias_resolves(self):
        """The `protected-planet` alias resolves to the same class."""
        assert EarthLens.DataSources["protected-planet"] is earthlens.wdpa.WDPA

    def test_facade_builds_wdpa_backend(self, tmp_path: Path):
        """The facade binds a WDPA instance as its datasource."""
        assert isinstance(_make_facade(tmp_path).datasource, earthlens.wdpa.WDPA)


@pytest.mark.wdpa
class TestFacadeDownload:
    """The facade rejects `aggregate=` and returns the FeatureCollection."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the vector backend raises."""
        with pytest.raises(NotImplementedError, match="vector"):
            _make_facade(tmp_path).download(aggregate=object())

    def test_download_returns_feature_collection(self, tmp_path: Path, fake_wdpa):
        """A facade download returns the protected-area FeatureCollection."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        fc = _make_facade(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1
