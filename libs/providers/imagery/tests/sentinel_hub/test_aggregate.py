"""Unit tests for the Sentinel Hub `aggregate=` windowed reduction (C10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from earthlens.aggregate import AggregationConfig
from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub


def _backend(output_dir, **kwargs) -> SentinelHub:
    """A small Process backend over a 3-day span."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-03",
        variables={"sentinel-2-l2a-ndvi": []},
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        resolution=10,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestRasterWindows:
    """Raster planes render one stamped output per window."""

    def test_daily_windows_over_three_days(self, fake_sh, output_dir: Path):
        """A daily aggregate over a 3-day span yields three stamped GeoTIFFs."""
        paths = _backend(output_dir).download(
            aggregate=AggregationConfig(freq="D", op="mean")
        )
        assert len(paths) == 3
        names = sorted(Path(p).name for p in paths)
        assert names[0].startswith("sentinel-2-l2a-ndvi_D_2020060")
        assert all(Path(p).exists() for p in paths)

    def test_one_render_per_window(self, fake_sh, output_dir: Path):
        """One Process request is issued per window."""
        _backend(output_dir).download(aggregate=AggregationConfig(freq="D", op="mean"))
        assert len(fake_sh.SentinelHubRequest.instances) == 3

    def test_window_intervals_advance(self, fake_sh, output_dir: Path):
        """Each window's request carries a distinct time interval."""
        _backend(output_dir).download(aggregate=AggregationConfig(freq="D", op="mean"))
        intervals = {
            req.input_data[0]["time_interval"]
            for req in fake_sh.SentinelHubRequest.instances
        }
        assert len(intervals) == 3


class TestStatisticalAggregate:
    """The statistical plane uses aggregation_interval, not a window loop."""

    def test_no_window_loop_for_statistical(self, fake_sh, output_dir: Path):
        """A statistical aggregate issues one request (server returns intervals)."""
        polygon = {
            "type": "Polygon",
            "coordinates": [[[14, 40], [14.1, 40], [14.1, 40.1], [14, 40.1], [14, 40]]],
        }
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-30",
            variables={"sentinel-2-l2a-ndvi-stats": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=output_dir,
            api="statistical",
            geometry=polygon,
            client_id="a",
            client_secret="b",
        )
        backend.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        assert len(fake_sh.SentinelHubStatistical.instances) == 1
        req = fake_sh.SentinelHubStatistical.instances[-1]
        assert req.aggregation["aggregation_interval"] == "P1M"
