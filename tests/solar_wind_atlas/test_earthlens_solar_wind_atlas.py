"""Tests for the `EarthLens` facade entries routing to the SolarWindAtlas backend."""

from __future__ import annotations

import pytest

import earthlens.solar_wind_atlas
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.solar_wind_atlas

KEYS = ["solar-wind-atlas", "global-solar-atlas", "global-wind-atlas", "gsa", "gwa"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the solar_wind_atlas entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every solar_wind_atlas key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_solar_wind_atlas_class(self, key: str) -> None:
        """All keys resolve to `earthlens.solar_wind_atlas.SolarWindAtlas`."""
        assert (
            EarthLens.DataSources[key] is earthlens.solar_wind_atlas.SolarWindAtlas
        )

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """The atlas backend needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="solar-wind-atlas", ...)`."""

    def test_constructs_backend(self, tmp_path) -> None:
        """The facade builds the `SolarWindAtlas` backend for an atlas request."""
        el = EarthLens(
            data_source="solar-wind-atlas",
            variables=["ghi", "wind_100m"],
            lat_lim=[55.0, 55.5],
            lon_lim=[12.0, 12.5],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.solar_wind_atlas.SolarWindAtlas)
        assert el.datasource.OUTPUT_KIND == "raster"
        assert [layer.id for layer in el.datasource._layers] == ["ghi", "wind_100m"]

    def test_gwa_alias_routes_to_backend(self, tmp_path) -> None:
        """`data_source="gwa"` constructs the `SolarWindAtlas` backend."""
        el = EarthLens(
            data_source="gwa",
            variables=["wind_100m"],
            lat_lim=[55.0, 55.5],
            lon_lim=[12.0, 12.5],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.solar_wind_atlas.SolarWindAtlas)

    def test_facade_download_returns_paths(
        self, fake_pyramids, fake_get, tmp_path
    ) -> None:
        """A faked backend routes through the facade and returns a list[Path]."""
        paths = EarthLens(
            data_source="gsa",
            variables=["ghi"],
            lat_lim=[55.0, 55.5],
            lon_lim=[12.0, 12.5],
            path=tmp_path,
        ).download()
        assert paths == [tmp_path / "ghi.tif"]
