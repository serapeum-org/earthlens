"""Live end-to-end tests for the GDACS multi-hazard alert backend.

Hits the real GDACS SEARCH feed, which is public, so these tests are
gated only behind the `e2e` pytest marker plus network availability — no
credentials are needed. A default `pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m e2e tests/gdacs
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.gdacs import GDACS
from earthlens.gdacs.events import ATTRIBUTE_COLUMNS

# A recent ~30-day window: GDACS is a live alert feed, so very old
# windows can be sparse. Earthquakes are the most frequent hazard, so a
# month of global EQ alerts is reliably non-empty.
_TODAY = dt.date.today()
_RECENT_START = (_TODAY - dt.timedelta(days=30)).strftime("%Y-%m-%d")
_TODAY_STR = _TODAY.strftime("%Y-%m-%d")


@pytest.mark.e2e
@pytest.mark.gdacs
class TestGdacsLiveQuery:
    """Live GDACS SEARCH queries (public — no credentials needed)."""

    def test_recent_earthquakes(self, tmp_path: Path):
        """A recent global EQ window returns a schema-correct FeatureCollection."""
        fc = EarthLens(
            variables=["EQ"],
            data_source="gdacs",
            start=_RECENT_START,
            end=_TODAY_STR,
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path=str(tmp_path),
        ).download(progress_bar=False)

        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert fc.crs.to_epsg() == 4326
        if len(fc):
            assert fc["hazard_type"].isin(["EQ"]).all(), "only EQ alerts requested"
            assert list(tmp_path.glob("gdacs_alerts_*.gpkg")), "GeoPackage written"

    def test_single_request(self, tmp_path: Path):
        """A multi-hazard download issues exactly one HTTP request (no fan-out)."""
        from unittest import mock

        import earthlens.gdacs.backend as backend_module

        with mock.patch.object(
            backend_module.requests, "get", wraps=backend_module.requests.get
        ) as spy:
            GDACS(
                start=_RECENT_START,
                end=_TODAY_STR,
                variables=["EQ", "TC", "FL", "VO", "WF", "DR"],
                lat_lim=[-90.0, 90.0],
                lon_lim=[-180.0, 180.0],
                path=str(tmp_path),
            ).download(progress_bar=False)
        assert spy.call_count == 1, (
            f"expected a single SEARCH request, got {spy.call_count}"
        )
