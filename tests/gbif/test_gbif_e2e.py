"""Live end-to-end test for the GBIF occurrence backend.

Hits the real GBIF occurrence API (anonymous, no credentials) but needs
`pygbif` installed and network access, so it is gated behind the `e2e`
marker and a skip when `pygbif` is absent. A default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m e2e tests/gbif
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from geopandas import GeoDataFrame

from earthlens.earthlens import EarthLens

_HAVE_PYGBIF = importlib.util.find_spec("pygbif") is not None


@pytest.mark.e2e
@pytest.mark.gbif
@pytest.mark.skipif(not _HAVE_PYGBIF, reason="install pygbif to run live GBIF e2e")
class TestGbifLiveQuery:
    """Live GBIF occurrence search (anonymous; needs pygbif + network)."""

    def test_small_bird_search(self, tmp_path: Path):
        """A tiny bbox + one year of bird records returns plausible points."""
        fc = EarthLens(
            data_source="gbif",
            start="2020-01-01",
            end="2020-12-31",
            variables=["birds"],
            lat_lim=[51.4, 51.6],
            lon_lim=[-0.2, 0.0],
            path=str(tmp_path),
            max_records=50,
        ).download(progress_bar=False)

        assert isinstance(fc, GeoDataFrame)
        assert len(fc) >= 1, "expected at least one bird occurrence in London"
        assert fc.crs.to_epsg() == 4326
