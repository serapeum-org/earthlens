"""Live end-to-end tests for the OpenStreetMap backend.

Hits the real Overpass + ohsome services, both public, so these tests are
gated only behind the `e2e` pytest marker plus network availability — no
credentials are needed. A default `pytest` invocation skips them; a connection
failure (offline / a throttled mirror) skips rather than fails.

Run with:

    pixi run -e dev pytest -m "osm and e2e" tests/osm
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.osm]

# A tiny bbox over central Heidelberg — dense, stable OSM coverage, so both a
# current-state Overpass query and an ohsome history snapshot reliably return
# features without straining the public services.
_LAT_LIM = [49.40, 49.42]
_LON_LIM = [8.67, 8.71]


def _skip_on_network(exc: Exception) -> None:
    """Skip (not fail) when the failure is a transport problem, else re-raise."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        pytest.skip(f"OSM service unreachable: {exc}")
    raise exc


class TestOverpassLive:
    """A live Overpass named query over a tiny bbox."""

    def test_hospitals_returns_features(self, tmp_path: Path):
        """overpass:hospitals over Heidelberg returns >=1 feature, EPSG:4326."""
        try:
            fc = EarthLens(
                data_source="osm",
                variables=["overpass:hospitals"],
                lat_lim=_LAT_LIM,
                lon_lim=_LON_LIM,
                path=str(tmp_path),
            ).download(progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - transport -> skip, else re-raise
            _skip_on_network(exc)
        assert len(fc) >= 1, "expected at least one hospital feature"
        assert fc.crs.to_epsg() == 4326
        assert {"osm_id", "osm_type"} <= set(fc.columns)


class TestOhsomeLive:
    """A live ohsome geometry query over a small bbox + time range."""

    def test_buildings_snapshot_returns_features(self, tmp_path: Path):
        """ohsome:buildings at a 2020 snapshot returns >=1 polygon feature."""
        try:
            fc = EarthLens(
                data_source="osm",
                variables=["ohsome:buildings"],
                lat_lim=_LAT_LIM,
                lon_lim=_LON_LIM,
                start="2020-01-01",
                path=str(tmp_path),
            ).download(progress_bar=False)
        except Exception as exc:  # noqa: BLE001 - transport -> skip, else re-raise
            _skip_on_network(exc)
        assert len(fc) >= 1, "expected at least one building feature"
        assert fc.crs.to_epsg() == 4326
        assert "@osmId" in fc.columns
