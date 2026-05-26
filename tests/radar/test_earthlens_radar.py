"""Facade-routing tests for the radar / nexrad keys."""

from __future__ import annotations

import pytest

import earthlens.radar
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.radar, pytest.mark.unit]


class TestRegistry:
    """Tests for the ``"radar"`` / ``"nexrad"`` registry entries."""

    def test_keys_present(self):
        """Both radar keys are registered."""
        assert "radar" in EarthLens.DataSources
        assert "nexrad" in EarthLens.DataSources

    def test_keys_resolve_to_radar(self):
        """Both keys resolve to earthlens.radar.Radar."""
        assert EarthLens.DataSources["radar"] is earthlens.radar.Radar
        assert EarthLens.DataSources["nexrad"] is earthlens.radar.Radar

    def test_facade_constructs_radar(self, tmp_path):
        """EarthLens(data_source='radar', ...) builds a Radar instance."""
        lens = EarthLens(
            data_source="radar",
            variables={"KTLX": []},
            start="2024-06-01T00:00:00",
            end="2024-06-01T23:59:59",
            lat_lim=[33, 37],
            lon_lim=[-100, -95],
            path=str(tmp_path),
            fmt="%Y-%m-%dT%H:%M:%S",
        )
        assert type(lens.datasource).__name__ == "Radar"
        assert lens.datasource.OUTPUT_KIND == "vector"

    def test_facade_rejects_aggregate_for_vector(self, tmp_path):
        """The facade rejects aggregate= for a vector backend before fetch."""
        lens = EarthLens(
            data_source="radar",
            variables={"KTLX": []},
            start="2024-06-01T00:00:00",
            end="2024-06-01T23:59:59",
            lat_lim=[33, 37],
            lon_lim=[-100, -95],
            path=str(tmp_path),
            fmt="%Y-%m-%dT%H:%M:%S",
        )
        with pytest.raises(NotImplementedError, match="aggregate="):
            lens.download(aggregate=object())
