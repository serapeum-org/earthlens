"""Facade-routing tests for the NWP backend key."""

from __future__ import annotations

import pytest

import earthlens.nwp
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


class TestRegistry:
    """Tests for the `"nwp"` registry entry on the facade."""

    def test_key_present(self):
        """The nwp key is registered alongside the other backends."""
        assert "nwp" in EarthLens.DataSources

    def test_key_resolves_to_nwp_class(self):
        """The nwp key resolves to earthlens.nwp.NWP."""
        assert EarthLens.DataSources["nwp"] is earthlens.nwp.NWP

    def test_facade_constructs_nwp(self, mini_catalog, tmp_path):
        """EarthLens(data_source='nwp', ...) builds an NWP bound to the catalog."""
        lens = EarthLens(
            data_source="nwp",
            variables={"gfs": ["temperature_2m"]},
            start="2024-06-01",
            end="2024-06-01",
            lat_lim=[10, 20],
            lon_lim=[30, 40],
            path=str(tmp_path),
            catalog=mini_catalog,
        )
        assert type(lens.datasource).__name__ == "NWP"
        assert lens.datasource.OUTPUT_KIND == "raster"
