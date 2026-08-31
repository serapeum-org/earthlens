"""Tests for the `EarthLens` facade entries that route to the DEM backend."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.dem
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.dem

KEYS = ["dem", "copernicus-dem", "cop-dem", "dem:elevation"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the DEM entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every DEM key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_dem_class(self, key: str) -> None:
        """All keys resolve to `earthlens.dem.DEM`."""
        assert EarthLens.DataSources[key] is earthlens.dem.DEM

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_s3_extra(self, key: str) -> None:
        """The DEM backend reuses the `s3` extra for the unsigned boto3 client."""
        entries = dict(
            (name, extra) for name, _mod, extra in EarthLens.DataSources.entries()
        )
        assert entries[key] == "s3"


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="dem", ...)`."""

    def test_constructs_dem_backend(self, tmp_path: Path) -> None:
        """The facade builds the `DEM` backend for a DEM request."""
        el = EarthLens(
            data_source="dem",
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.dem.DEM)
        assert el.datasource.OUTPUT_KIND == "raster"
        assert el.datasource._dataset_key == "cop-dem-glo-30"

    def test_dataset_kwarg_forwarded(self, tmp_path: Path) -> None:
        """Passing `dataset="cop-dem-glo-90"` picks the 90-m bucket."""
        el = EarthLens(
            data_source="dem",
            dataset="cop-dem-glo-90",
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        assert el.datasource._dataset.bucket == "copernicus-dem-90m"

    def test_elevation_alias_routes_to_dem(self, tmp_path: Path) -> None:
        """`data_source="dem:elevation"` constructs the `DEM` backend."""
        el = EarthLens(
            data_source="dem:elevation",
            variables=[],
            lat_lim=[30.2, 30.8],
            lon_lim=[31.2, 31.8],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.dem.DEM)
