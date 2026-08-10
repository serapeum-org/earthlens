"""Live end-to-end tests for the HANZE historical-flood-impacts backend.

Hits the real Zenodo record `20478847`, which is public (CC-BY-4.0), so these
tests are gated only behind the `e2e` pytest marker plus network availability —
no credentials are needed. A default `pytest` invocation skips them.

Run with:

    pytest -m "e2e and hanze" libs/providers/hazards/tests/hanze
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.core import EarthLens


@pytest.mark.e2e
@pytest.mark.hanze
class TestHanzeLiveDownload:
    """Live downloads from the pinned HANZE Zenodo record (public)."""

    def test_events_dataframe(self, tmp_path: Path) -> None:
        """A DE+NL window returns a real event/impact DataFrame with HANZE columns."""
        events = EarthLens(
            "hanze",
            start="1950",
            end="2020",
            country=["DE", "NL"],
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(events, pd.DataFrame)
        assert len(events) > 0, "expected some DE+NL floods in 1950-2020"
        for column in ("Country code", "Year", "Type", "Fatalities"):
            assert column in events.columns, f"missing HANZE column {column!r}"
        assert set(events["Country code"].unique()) <= {"DE", "NL"}
        assert events["Year"].between(1950, 2020).all()
        assert list(tmp_path.glob("hanze_events-*.csv")), "events CSV written"

    def test_affected_region_geometry(self, tmp_path: Path) -> None:
        """`with_geometry` returns a real region FeatureCollection in WGS84."""
        regions = EarthLens(
            "hanze",
            start="1990",
            end="2020",
            country="DE",
            with_geometry=True,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert isinstance(regions, FeatureCollection)
        assert len(regions) > 0, "expected affected NUTS-3 regions"
        assert regions.crs.to_epsg() == 4326
        assert set(regions.columns) >= {"nuts3_code", "region_name", "n_events"}
        assert (regions["n_events"] >= 1).all()
        assert regions["nuts3_code"].str.startswith("DE").all()
