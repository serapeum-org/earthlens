"""Integration tests for the Sentinel Hub backend behind the `EarthLens` facade."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens
from earthlens.sentinel_hub import SentinelHub

pytestmark = pytest.mark.sentinel_hub


class TestFacadeRouting:
    """Both Sentinel Hub keys route to the backend."""

    def test_keys_registered(self):
        """`"sentinel-hub"` and the `"sentinelhub"` alias are registered."""
        assert "sentinel-hub" in EarthLens.DataSources
        assert "sentinelhub" in EarthLens.DataSources

    def test_keys_resolve_to_backend(self):
        """Both keys resolve to the SentinelHub backend class."""
        assert EarthLens.DataSources["sentinel-hub"] is SentinelHub
        assert EarthLens.DataSources["sentinelhub"] is SentinelHub

    def test_facade_constructs_backend(self, tmp_path: Path):
        """The facade builds a SentinelHub instance bound to `datasource`."""
        facade = EarthLens(
            data_source="sentinel-hub",
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=tmp_path,
        )
        assert isinstance(facade.datasource, SentinelHub)

    def test_backend_kwargs_forwarded(self, tmp_path: Path):
        """Backend-specific kwargs (resolution, api) reach the constructor."""
        facade = EarthLens(
            data_source="sentinelhub",
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=tmp_path,
            resolution=20.0,
            api="process",
        )
        assert facade.datasource._resolution == 20.0
        assert facade.datasource._api == "process"


class TestFacadeDownload:
    """The facade forwards downloads (and `aggregate=`) for the mixed backend."""

    def _facade(self, tmp_path: Path, **kwargs) -> EarthLens:
        """Build a facade-bound Sentinel Hub backend (creds via kwargs)."""
        return EarthLens(
            data_source="sentinel-hub",
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=tmp_path,
            client_id="a",
            client_secret="b",
            **kwargs,
        )

    def test_download_returns_paths(self, fake_sh, tmp_path: Path):
        """`download()` renders and returns the written paths."""
        paths = self._facade(tmp_path).download()
        assert len(paths) == 1 and Path(paths[0]).exists()

    def test_aggregate_not_rejected_for_mixed(self, fake_sh, tmp_path: Path):
        """`aggregate=` is forwarded (mixed), not rejected by the facade."""
        facade = self._facade(tmp_path)
        facade.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
