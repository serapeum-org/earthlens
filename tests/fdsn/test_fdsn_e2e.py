"""Live end-to-end tests for the FDSN seismic-event backend.

Hits the real FDSN event web services. All bundled networks expose
public event services, so these tests are gated only behind the `e2e`
pytest marker plus network availability; a default `pytest` invocation
skips them.

Run with:

    pixi run -e dev pytest -m e2e tests/fdsn
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens
from earthlens.fdsn import FDSN
from earthlens.fdsn.events import ATTRIBUTE_COLUMNS

# A historically very active window + box: the 2011 Tohoku sequence off
# the Pacific coast of Japan, which has many M5+ events on record.
_ACTIVE_START = "2011-03-11"
_ACTIVE_END = "2011-03-18"
_JAPAN_LAT = [30.0, 45.0]
_JAPAN_LON = [135.0, 150.0]


@pytest.mark.e2e
@pytest.mark.fdsn
class TestUsgsLiveQuery:
    """Live USGS ComCat queries (public — no credentials needed)."""

    def test_active_window_returns_events(self, tmp_path: Path):
        """A known active window/box returns plausible M5+ events."""
        fc = EarthLens(
            variables=["USGS"],
            data_source="fdsn",
            start=_ACTIVE_START,
            end=_ACTIVE_END,
            lat_lim=_JAPAN_LAT,
            lon_lim=_JAPAN_LON,
            path=str(tmp_path),
            min_magnitude=5.0,
        ).download()

        assert len(fc) > 0, "expected at least one M5+ event in the Tohoku window"
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"
        assert fc.crs.to_epsg() == 4326
        assert (fc["magnitude"].dropna() >= 5.0).all(), "min_magnitude not honoured"
        assert (fc["depth_km"].dropna() >= 0).all(), "negative depth_km"
        assert fc["latitude"].between(_JAPAN_LAT[0], _JAPAN_LAT[1]).all()
        assert fc["longitude"].between(_JAPAN_LON[0], _JAPAN_LON[1]).all()
        assert (tmp_path / "usgs.gpkg").is_file(), "GeoPackage should be written"

    def test_quiet_query_returns_empty_but_valid(self, tmp_path: Path):
        """A deliberately quiet box/time returns an empty, schema-correct FC."""
        fc = FDSN(
            start="2020-01-01",
            end="2020-01-02",
            variables=["USGS"],
            lat_lim=[0.0, 0.5],
            lon_lim=[0.0, 0.5],
            path=str(tmp_path),
            min_magnitude=8.0,
        ).download()

        assert len(fc) == 0, "no M8+ events expected in a half-degree box over 1 day"
        for column in ATTRIBUTE_COLUMNS:
            assert column in fc.columns, f"missing column {column!r}"


@pytest.mark.e2e
@pytest.mark.fdsn
class TestEarthscopeLiveQuery:
    """Live EarthScope query (public event service — no token needed)."""

    def test_active_window_returns_events(self, tmp_path: Path):
        """EarthScope returns plausible events for the active window."""
        fc = FDSN(
            start=_ACTIVE_START,
            end=_ACTIVE_END,
            variables=["EARTHSCOPE"],
            lat_lim=_JAPAN_LAT,
            lon_lim=_JAPAN_LON,
            path=str(tmp_path),
            min_magnitude=5.0,
        ).download()

        assert len(fc) > 0, "expected at least one M5+ event from EarthScope"
        assert fc.crs.to_epsg() == 4326
