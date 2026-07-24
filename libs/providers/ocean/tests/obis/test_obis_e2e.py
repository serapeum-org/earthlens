"""Live end-to-end test for the OBIS marine-occurrence backend.

Hits the real OBIS occurrence API (anonymous, no credentials) but needs
`pyobis` installed and network access, so it is gated behind the `e2e`
marker and a skip when `pyobis` is absent. A default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m e2e tests/obis
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from geopandas import GeoDataFrame

from earthlens.earthlens import EarthLens

_HAVE_PYOBIS = importlib.util.find_spec("pyobis") is not None


@pytest.mark.e2e
@pytest.mark.obis
@pytest.mark.skipif(not _HAVE_PYOBIS, reason="install pyobis to run live OBIS e2e")
class TestObisLiveQuery:
    """Live OBIS occurrence search (anonymous; needs pyobis + network)."""

    def test_small_sunfish_search(self, tmp_path: Path):
        """A North-Atlantic bbox + sunfish records returns plausible points."""
        fc = EarthLens(
            data_source="obis",
            start="2010-01-01",
            end="2020-12-31",
            variables=["ocean-sunfish"],
            lat_lim=[40.0, 55.0],
            lon_lim=[-20.0, 0.0],
            path=str(tmp_path),
            size=50,
        ).download(progress_bar=False)

        assert isinstance(fc, GeoDataFrame)
        assert len(fc) >= 1, "expected at least one Mola mola occurrence"
        assert fc.crs.to_epsg() == 4326
