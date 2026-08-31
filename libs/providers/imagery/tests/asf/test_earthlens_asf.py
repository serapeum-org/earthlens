"""Tests for the `EarthLens` facade entries that route to the ASF backend."""

from __future__ import annotations

import pytest

import earthlens.asf
from earthlens.earthlens import EarthLens


@pytest.mark.asf
@pytest.mark.unit
class TestRegistry:
    """Tests for the ASF entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", ["asf", "alaska-satellite-facility", "asf:insar"])
    def test_keys_present(self, key: str) -> None:
        """Every ASF key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", ["asf", "alaska-satellite-facility", "asf:insar"])
    def test_keys_resolve_to_asf_class(self, key: str) -> None:
        """All three keys resolve to `earthlens.asf.ASF`."""
        assert EarthLens.DataSources[key] is earthlens.asf.ASF


@pytest.mark.asf
@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="asf", ...)`."""

    def test_constructs_asf_backend_search_mode(self, fake_asf_search, tmp_path):
        """The facade builds the `ASF` backend in search mode."""
        el = EarthLens(
            data_source="asf",
            variables=["sentinel-1-slc"],
            start="2024-01-01",
            end="2024-01-31",
            lat_lim=[40.0, 41.0],
            lon_lim=[-100.0, -99.0],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.asf.ASF)
        assert el.datasource._product_key == "sentinel-1-slc"

    def test_insar_alias_routes_to_asf(self, fake_asf_search, tmp_path):
        """`data_source="asf:insar"` constructs the `ASF` backend too."""
        el = EarthLens(
            data_source="asf:insar",
            variables=["sentinel-1-slc"],
            reference="S1A_REF_SLC",
            start="2024-01-01",
            end="2024-01-31",
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.asf.ASF)
        assert el.datasource._mode == "stack"
