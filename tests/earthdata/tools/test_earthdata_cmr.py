"""Unit tests for the network-free Earthdata tooling helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.earthdata, pytest.mark.unit]

_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "earthdata"


def _load(module_name: str, filename: str):
    """Import a tools/earthdata module by path (not on sys.path)."""
    sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(module_name, _TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cmr = _load("_earthdata_cmr", "_earthdata_cmr.py")


class TestFormatFromExtension:
    """format_from_extension maps suffixes to coarse labels."""

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("a.nc4", "netcdf4"),
            ("b.HDF", "hdf-eos2"),
            ("c.tif", "cog"),
            ("d.csv", "csv"),
            ("e.gpkg", "geopackage"),
            ("f.unknown", ""),
            ("g.nc?token=x", "netcdf4"),
        ],
    )
    def test_maps_suffix(self, name, expected):
        """Each known suffix maps to its label; query strings are stripped."""
        assert cmr.format_from_extension(name) == expected


class TestInferOutputKind:
    """infer_output_kind seeds raster/vector/tabular."""

    def test_raster_default(self):
        """A gridded product defaults to raster."""
        assert cmr.infer_output_kind("GPM_3IMERGHHL", "hdf5") == "raster"

    def test_gedi_is_vector(self):
        """GEDI footprints are vector."""
        assert cmr.infer_output_kind("GEDI04_A", "hdf5") == "vector"

    def test_icesat2_is_vector(self):
        """ICESat-2 ATL08 photon products are vector."""
        assert cmr.infer_output_kind("ATL08", "hdf5") == "vector"

    def test_csv_is_tabular(self):
        """A CSV product is tabular."""
        assert cmr.infer_output_kind("FLUXNET_CH4", "csv") == "tabular"

    def test_geopackage_is_vector(self):
        """A GeoPackage product is vector, not tabular."""
        assert cmr.infer_output_kind("SOME_VECTOR", "geopackage") == "vector"


class TestReadProviders:
    """read_providers loads the bundled registry."""

    def test_lists_nine_daacs(self):
        """The registry covers all nine user-relevant DAACs."""
        providers = cmr.read_providers()
        assert len(providers) == 9
        assert "GES_DISC" in providers and providers["POCLOUD"]["daac"] == "PO.DAAC"
