"""Live end-to-end tests for the glaciers backend.

Hits the three real, open sources — RGI 7.0 region shapefiles on UNESCO IHP-WINS,
the GLIMS GeoServer WFS, and the WGMS Fluctuations of Glaciers archive — gated
only on the `e2e` marker plus a per-host network-reachability skip (no
credentials: every source is open). A default `pytest` run skips them all.

Run with:

    pixi run -e dev pytest -m "e2e and glaciers"
"""

from __future__ import annotations

import socket
from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.glaciers]


def _host_ok(host: str) -> bool:
    """Return True when `host` is reachable on port 443."""
    try:
        socket.create_connection((host, 443), timeout=5).close()
        return True
    except OSError:
        return False


_rgi_skip = pytest.mark.skipif(
    not _host_ok("ihp-wins.unesco.org"), reason="IHP-WINS host unreachable"
)
_glims_skip = pytest.mark.skipif(
    not _host_ok("www.glims.org"), reason="GLIMS host unreachable"
)
_wgms_skip = pytest.mark.skipif(not _host_ok("wgms.ch"), reason="WGMS host unreachable")

# A small bbox over the French Alps (region 11, Central Europe).
ALPS_LAT = [45.8, 46.0]
ALPS_LON = [6.8, 7.1]


@_rgi_skip
def test_rgi_live_returns_clipped_outlines(tmp_path: Path) -> None:
    """A live RGI pull over the Alps returns clipped EPSG:4326 outlines."""
    fc = EarthLens(
        data_source="glaciers",
        variables=["rgi:outlines"],
        lat_lim=ALPS_LAT,
        lon_lim=ALPS_LON,
        path=str(tmp_path),
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"
    assert len(fc) >= 1
    assert "rgi_id" in fc.columns


@_glims_skip
def test_glims_live_returns_outlines(tmp_path: Path) -> None:
    """A live GLIMS WFS bbox query returns time-series outlines."""
    fc = EarthLens(
        data_source="glims",
        variables=["glims:outlines"],
        lat_lim=ALPS_LAT,
        lon_lim=ALPS_LON,
        max_features=50,
        path=str(tmp_path),
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"
    assert len(fc) >= 1


@_wgms_skip
def test_wgms_live_returns_mass_balance(tmp_path: Path) -> None:
    """A live WGMS pull returns a non-empty mass-balance table for one glacier."""
    df = EarthLens(
        data_source="wgms",
        variables=["wgms:mass_balance"],
        region="11",
        path=str(tmp_path),
    ).download()
    assert isinstance(df, pd.DataFrame)
    assert len(df) >= 1
    assert {"glacier_id", "year", "annual_balance"} <= set(df.columns)
