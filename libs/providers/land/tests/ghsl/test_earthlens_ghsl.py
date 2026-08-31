"""Facade-level tests for the GHSL backend via `EarthLens`."""

from __future__ import annotations

import pytest

import earthlens.ghsl
from earthlens.earthlens import EarthLens


@pytest.mark.ghsl
class TestFacadeRouting:
    """`EarthLens(data_source=...)` routing for the GHSL keys."""

    @pytest.mark.parametrize("key", ["ghsl", "ghs", "ghsl:human-settlement"])
    def test_keys_registered(self, key):
        """Each GHSL key resolves to `earthlens.ghsl.GHSL`."""
        assert key in EarthLens.DataSources
        assert EarthLens.DataSources[key] is earthlens.ghsl.GHSL

    def test_no_extra_required(self):
        """The [ghsl] extra is empty (direct path needs only core deps)."""
        assert EarthLens.DataSources.default_kwargs("ghsl") == {}

    def test_facade_constructs_backend(self, tmp_path):
        """The facade builds a GHSL instance and forwards backend kwargs."""
        el = EarthLens(
            data_source="ghsl",
            variables=["GHS_POP"],
            start="2020-01-01",
            end="2020-12-31",
            lat_lim=[30.5, 31.0],
            lon_lim=[-9.0, -8.5],
            path=str(tmp_path),
            resolution="100m",
        )
        assert isinstance(el.datasource, earthlens.ghsl.GHSL)
        assert el.datasource.OUTPUT_KIND == "raster"
