"""Tests for the `EarthLens` facade entries that route to the FABDEM backend."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.fabdem
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.fabdem

KEYS = ["fabdem", "fab-dem", "fabdem:bare-earth-dem"]

#: The FABDEM src package files that must never import xarray (raster I/O is pyramids').
_SRC_DIR = Path(earthlens.fabdem.__file__).parent


@pytest.mark.unit
class TestRegistry:
    """Tests for the FABDEM entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every FABDEM key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_fabdem_class(self, key: str) -> None:
        """All keys resolve to `earthlens.fabdem.FABDEM`."""
        assert EarthLens.DataSources[key] is earthlens.fabdem.FABDEM

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_hint_no_extra(self, key: str) -> None:
        """FABDEM needs no optional extra, so its hint is empty."""
        assert EarthLens.DataSources.default_kwargs(key) == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="fabdem", ...)`."""

    def test_constructs_fabdem_backend(self, tmp_path):
        """The facade builds the `FABDEM` backend for a DEM request."""
        el = EarthLens(
            data_source="fabdem",
            lat_lim=[50.4, 50.6],
            lon_lim=[0.4, 0.6],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.fabdem.FABDEM)
        assert el.datasource.OUTPUT_KIND == "raster"

    def test_alias_routes_to_fabdem(self, tmp_path):
        """`data_source="fabdem:bare-earth-dem"` constructs the `FABDEM` backend."""
        el = EarthLens(
            data_source="fabdem:bare-earth-dem",
            lat_lim=[50.4, 50.6],
            lon_lim=[0.4, 0.6],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.fabdem.FABDEM)


@pytest.mark.unit
class TestNoXarray:
    """FABDEM does its raster I/O through pyramids, never xarray."""

    def test_no_xarray_import(self) -> None:
        """No FABDEM src module imports xarray."""
        offenders = [
            path.name
            for path in _SRC_DIR.glob("*.py")
            if "import xarray" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"xarray imported in: {offenders}"
