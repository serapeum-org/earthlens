"""Live end-to-end test for the Protected Planet (WDPA) backend.

Hits the real Protected Planet v4 API, which requires a personal token, so
it is gated behind the `e2e` marker and a skip on a missing `WDPA_TOKEN`. A
default `pytest` run skips it. Request a token at
`https://api.protectedplanet.net/request`.

Run with:

    uv run --active pytest -m "e2e and wdpa" libs/providers/land/tests/wdpa
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import MultiPolygon, Polygon

from earthlens.earthlens import EarthLens

_HAVE_TOKEN = bool(os.environ.get("WDPA_TOKEN"))


@pytest.mark.e2e
@pytest.mark.wdpa
@pytest.mark.skipif(not _HAVE_TOKEN, reason="set WDPA_TOKEN to run live WDPA e2e")
class TestWdpaLiveQuery:
    """Live Protected Planet v4 queries (require a WDPA_TOKEN)."""

    def test_country_protected_areas(self, tmp_path: Path):
        """One small country's protected areas come back as polygons."""
        fc = EarthLens(
            data_source="wdpa",
            start="2024-01-01",
            end="2024-12-31",
            variables=["LIE"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(fc, GeoDataFrame)
        assert len(fc) >= 1, "expected at least one protected area"
        assert isinstance(fc.geometry.iloc[0], (Polygon, MultiPolygon))
        assert fc.crs.to_epsg() == 4326
