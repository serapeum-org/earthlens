"""The real GDACS exchange, recorded once and replayed offline.

The sibling fake-based tests cover the error branches a cassette cannot
reach. This one pins the shape of what GDACS actually sends back, so a
change in that shape fails here rather than in the weekly live lane.
"""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens


@pytest.mark.vcr
@pytest.mark.gdacs
def test_earthquake_search_returns_the_documented_columns(tmp_path):
    """A fixed EQ window replays to a schema-correct FeatureCollection."""
    fc = EarthLens(
        variables=["EQ"],
        data_source="gdacs",
        start="2026-09-01",
        end="2026-09-08",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
    ).download(progress_bar=False)

    assert fc.crs.to_epsg() == 4326
    assert "hazard_type" in fc.columns
