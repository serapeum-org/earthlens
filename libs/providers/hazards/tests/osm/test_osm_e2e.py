"""Live end-to-end tests for the OpenStreetMap backend.

Hits the real Overpass + ohsome services, both public, so these tests are
gated only behind the `e2e` pytest marker plus network availability — no
credentials are needed. A default `pytest` invocation skips them; a connection
failure (offline / a throttled mirror) skips rather than fails.

Run with:

    uv run pytest -m "osm and e2e" tests/osm
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from earthlens.earthlens import EarthLens
from earthlens.osm import OhsomeUnavailableError
from earthlens.testing import skip_live_unavailable, skip_on_upstream

pytestmark = [pytest.mark.e2e, pytest.mark.osm]

# A tiny bbox over central Heidelberg — dense, stable OSM coverage, so both a
# current-state Overpass query and an ohsome history snapshot reliably return
# features without straining the public services.
_LAT_LIM = [49.40, 49.42]
_LON_LIM = [8.67, 8.71]


def _skip_on_network(exc: Exception) -> NoReturn:
    """Skip an ohsome throttle/outage; let the shared classifier judge the rest.

    One arm is genuinely backend-specific and stays here: `api.ohsome.org` is
    keyless, so a `403` from its front proxy is a throttle (#1025) and a `5xx`
    on this tiny smoke bbox is an outage (#790) — neither is a regression. The
    generic status rule deliberately refuses to forgive a `403`, and widening
    it for everyone would mask real auth failures, so the exception is declared
    here rather than in the shared classifier. Equally deliberately, a `404` or
    a `600` on the same type re-raises: ohsome answers 4xx for a bad
    filter/bbox/time, and #1088 exists to keep that failing.

    Everything else goes to `skip_on_upstream`, which skips a transport dropout
    with the guard prefix and re-raises anything that is not an outage.
    """
    status = getattr(exc, "status_code", None)
    if (
        isinstance(exc, OhsomeUnavailableError)
        and status is not None
        and (status in (403, 429) or 500 <= status < 600)
    ):
        skip_live_unavailable(f"ohsome public endpoint unavailable (HTTP {status})")
    skip_on_upstream(exc)


class TestOverpassLive:
    """A live Overpass named query over a tiny bbox."""

    def test_hospitals_returns_features(self, tmp_path: Path):
        """live:hospitals over Heidelberg returns >=1 feature, EPSG:4326."""
        try:
            fc = EarthLens(
                data_source="osm",
                variables=["live:hospitals"],
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
        """history:buildings at a 2020 snapshot returns >=1 polygon feature."""
        try:
            fc = EarthLens(
                data_source="osm",
                variables=["history:buildings"],
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
