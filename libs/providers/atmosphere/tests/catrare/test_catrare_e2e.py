"""Gated end-to-end tests for the CatRaRE backend (live DWD open-data fetch).

Selected with `-m "e2e and catrare"`; the default suite deselects `e2e`. These
hit the real DWD open-data host, so they need network but no credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.catrare import CatRaRE
from earthlens.core import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.catrare]


def test_live_july_2021_events_over_germany(tmp_path: Path) -> None:
    """The July-2021 flood window over west Germany returns real events."""
    fc = CatRaRE(
        threshold="t5",
        start="2021-07-01",
        end="2021-07-31",
        lat_lim=[50.0, 51.5],
        lon_lim=[6.0, 8.0],
        path=tmp_path,
        cache_dir=tmp_path,
    ).download()

    assert isinstance(fc, FeatureCollection)
    assert len(fc) > 0
    assert fc.crs.to_epsg() == 4326  # reprojected off the RADOLAN grid
    assert "Eta" in fc.columns and "Area" in fc.columns
    assert list(tmp_path.glob("catrare_t5_zones_*.gpkg"))


def test_live_full_archive_is_large(tmp_path: Path) -> None:
    """The unfiltered T5 catalogue holds tens of thousands of events."""
    df = EarthLens(
        data_source="catrare", threshold="t5", geometry=False, cache_dir=tmp_path
    ).download()
    assert len(df) > 10000  # ~40k T5 events over 2001-2025
