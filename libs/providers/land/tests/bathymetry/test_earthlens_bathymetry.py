"""Tests for the `EarthLens` facade entries that route to the bathymetry backend."""

from __future__ import annotations

import pytest

import earthlens.bathymetry
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.bathymetry

KEYS = ["bathymetry", "gebco", "etopo"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the bathymetry entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every bathymetry key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_bathymetry_class(self, key: str) -> None:
        """All keys resolve to `earthlens.bathymetry.Bathymetry`."""
        assert EarthLens.DataSources[key] is earthlens.bathymetry.Bathymetry

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The DEM backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="bathymetry", ...)`."""

    def test_constructs_bathymetry_backend(self, tmp_path):
        """The facade builds the `Bathymetry` backend for a DEM request."""
        el = EarthLens(
            data_source="bathymetry",
            dataset="gebco_2020",
            lat_lim=[25.0, 26.0],
            lon_lim=[-18.0, -17.0],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.bathymetry.Bathymetry)
        assert el.datasource._dataset.id == "gebco_2020"
        assert el.datasource.OUTPUT_KIND == "raster"

    def test_etopo_alias_routes_to_bathymetry(self, tmp_path):
        """`data_source="etopo"` constructs the `Bathymetry` backend."""
        el = EarthLens(
            data_source="etopo",
            dataset="etopo1_bedrock",
            lat_lim=[25.0, 26.0],
            lon_lim=[-18.0, -17.0],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.bathymetry.Bathymetry)
        assert el.datasource._dataset.id == "etopo1_bedrock"

    def test_gebco_alias_routes_to_bathymetry(self, tmp_path):
        """`data_source="gebco"` constructs the `Bathymetry` backend."""
        el = EarthLens(
            data_source="gebco",
            dataset="gebco_2020",
            lat_lim=[25.0, 26.0],
            lon_lim=[-18.0, -17.0],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.bathymetry.Bathymetry)
