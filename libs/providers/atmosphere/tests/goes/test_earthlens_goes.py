"""Facade-wiring tests for the GOES backend via the EarthLens registry."""

from __future__ import annotations

import pytest
from earthlens.earthlens import EarthLens

from earthlens.goes import GOES

pytestmark = pytest.mark.goes


class TestFacadeWiring:
    """Tests that EarthLens routes the goes key to the GOES backend."""

    def test_key_present(self):
        """The goes key is registered in the EarthLens DataSources registry."""
        assert "goes" in EarthLens.DataSources, "goes must be a registered key"

    def test_key_resolves_to_goes(self):
        """DataSources['goes'] resolves to the GOES backend class."""
        assert EarthLens.DataSources["goes"] is GOES, "goes -> GOES"

    def test_key_extra_is_s3(self):
        """The goes key advertises the s3 extra (unsigned boto3)."""
        extras = {key: extra for key, _module, extra in EarthLens.DataSources.entries()}
        assert extras["goes"] == "s3", "goes rides the [s3] extra"

    def test_facade_constructs_backend(self, tmp_path):
        """EarthLens('goes', ...) constructs a GOES backend without the network."""
        earthlens = EarthLens(
            "goes",
            dataset="abi-l2-mcmip",
            variables=["CMI_C13"],
            start="2026-07-03",
            end="2026-07-03",
            lat_lim=[20, 50],
            lon_lim=[-130, -60],
            satellite="east",
            domain="C",
            path=str(tmp_path),
        )
        assert isinstance(earthlens.datasource, GOES), "facade binds the GOES backend"
        assert earthlens.datasource._bucket == "noaa-goes19", "east bucket resolved"
