"""Integration tests for the openEO backend behind the `EarthLens` facade."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

from earthlens.openeo import OpenEO

from .conftest import FakeAuth, FakeConnection


@pytest.mark.openeo
class TestFacadeRouting:
    """The `"openeo"` key routes to the OpenEO backend."""

    def test_key_registered(self):
        """`"openeo"` is a registered data source."""
        assert "openeo" in EarthLens.DataSources

    def test_key_resolves_to_openeo(self):
        """The key resolves to the OpenEO backend class."""
        assert EarthLens.DataSources["openeo"] is OpenEO

    def test_facade_constructs_backend(self, tmp_path: Path):
        """The facade builds an OpenEO instance bound to `datasource`."""
        facade = EarthLens(
            data_source="openeo",
            start="2023-01-01",
            end="2023-01-31",
            variables={"sentinel-2-l2a": ["B04"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[3.0, 4.0],
            path=tmp_path,
        )
        assert isinstance(facade.datasource, OpenEO)


@pytest.mark.openeo
class TestFacadeDownload:
    """The facade forwards downloads (and `aggregate=`) to the backend."""

    def _facade(self, tmp_path: Path, **kwargs) -> EarthLens:
        """Build a facade-bound openEO backend with a fake connection."""
        facade = EarthLens(
            data_source="openeo",
            start="2023-01-01",
            end="2023-03-31",
            variables={"sentinel-2-l2a": ["B04", "B08"]},
            lat_lim=[40.0, 41.0],
            lon_lim=[3.0, 4.0],
            path=tmp_path,
            **kwargs,
        )
        facade.datasource._auth = FakeAuth(FakeConnection())
        return facade

    def test_download_returns_paths(self, tmp_path: Path):
        """`download()` returns the written file paths."""
        paths = self._facade(tmp_path).download()
        assert len(paths) == 1 and Path(paths[0]).exists()

    def test_aggregate_forwarded_for_raster(self, tmp_path: Path):
        """`aggregate=` is forwarded (raster), not rejected by the facade."""
        facade = self._facade(tmp_path)
        conn = facade.datasource._auth.connection()
        facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        assert ("aggregate_temporal_period", "month", "mean") in conn.log

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """Backend-specific kwargs (max_cloud_cover) reach the constructor."""
        facade = self._facade(tmp_path, max_cloud_cover=20)
        assert facade.datasource._max_cloud_cover == 20
