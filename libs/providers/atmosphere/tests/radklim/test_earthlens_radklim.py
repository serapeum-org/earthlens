"""Facade-routing tests for the radklim / radolan keys."""

from __future__ import annotations

import pytest

import earthlens.radklim
from earthlens.earthlens import EarthLens

from .conftest import FakeHttp

pytestmark = [pytest.mark.radklim, pytest.mark.unit]


class TestRegistry:
    """Tests for the `"radklim"` / `"radolan"` registry entries."""

    def test_keys_present(self):
        """Both facade keys are registered."""
        assert "radklim" in EarthLens.DataSources
        assert "radolan" in EarthLens.DataSources

    def test_keys_resolve_to_radklim(self):
        """Both keys resolve to earthlens.radklim.RADKLIM."""
        assert EarthLens.DataSources["radklim"] is earthlens.radklim.RADKLIM
        assert EarthLens.DataSources["radolan"] is earthlens.radklim.RADKLIM

    def test_facade_constructs_radklim(self, tmp_path):
        """EarthLens(data_source='radklim', dataset=...) builds a RADKLIM instance."""
        lens = EarthLens(
            data_source="radklim",
            dataset="radklim-yw",
            start="2024-06-01",
            end="2024-06-02",
            lat_lim=[47, 55],
            lon_lim=[6, 15],
            path=str(tmp_path),
            client=FakeHttp(),
        )
        assert type(lens.datasource).__name__ == "RADKLIM"
        assert lens.datasource.OUTPUT_KIND == "raster"
