"""Tests for the `EarthLens` facade entries routing to the JRC backend."""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.jrc
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.jrc

KEYS = ["jrc", "jrc-flood", "efhm", "jrc-flood-hazard", "jrc:european-flood-hazard"]

#: The EFHM src package files that must never import xarray (raster I/O is pyramids').
_SRC_DIR = Path(earthlens.jrc.__file__).parent


@pytest.mark.unit
class TestRegistry:
    """Tests for the JRC entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every JRC key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_jrcflood_class(self, key: str) -> None:
        """All keys resolve to `earthlens.jrc.JRC`."""
        assert EarthLens.DataSources[key] is earthlens.jrc.JRC


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="jrc-flood", ...)`."""

    def test_constructs_backend_with_return_periods(self, tmp_path):
        """The facade builds JRC and forwards return_periods."""
        el = EarthLens(
            data_source="jrc-flood",
            lat_lim=[51.8, 52.0],
            lon_lim=[4.8, 5.0],
            return_periods=[100],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.jrc.JRC)
        assert el.datasource.OUTPUT_KIND == "raster"
        assert el.datasource._return_periods == [100]

    def test_alias_routes_to_backend(self, tmp_path):
        """`data_source="efhm"` constructs the JRC backend."""
        el = EarthLens(
            data_source="efhm",
            lat_lim=[51.8, 52.0],
            lon_lim=[4.8, 5.0],
            path=tmp_path,
        )
        assert isinstance(el.datasource, earthlens.jrc.JRC)


@pytest.mark.unit
class TestNoXarray:
    """JRC does its raster I/O through pyramids, never xarray."""

    def test_no_xarray_import(self) -> None:
        """Importing the JRC backend never pulls xarray in."""
        offenders = [
            path.name
            for path in _SRC_DIR.glob("*.py")
            if "import xarray" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"xarray imported in: {offenders}"
