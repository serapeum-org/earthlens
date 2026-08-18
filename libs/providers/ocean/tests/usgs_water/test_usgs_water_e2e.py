"""Live end-to-end tests for the USGS NWIS / Water Data backend.

Hits the real USGS service. Anonymous access works, so these are gated
only on the `e2e` marker plus a quick network-reachability skip (a
default `pytest` run skips them). The values / statistics / sites cases
use `api="legacy"` (the `waterservices.usgs.gov` endpoint, which serves
anonymous requests reliably); the modern-only `samples` case is gated on
`API_USGS_PAT` because `api.waterdata.usgs.gov` rate-limits anonymous
access.

Run with:

    pixi run -e dev pytest -m "e2e and usgs_water"
"""

from __future__ import annotations

import datetime as dt
import os
import socket
from pathlib import Path

import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.usgs_water]

#: Potomac River near Washington, DC — a long-running discharge gauge.
_SITE = "01646500"
_DC_LAT = [38.9, 39.0]
_DC_LON = [-77.2, -77.0]

_HAVE_TOKEN = bool(os.environ.get("API_USGS_PAT"))


def _network_ok() -> bool:
    """Return True when the legacy USGS host is reachable on 443."""
    try:
        socket.create_connection(("waterservices.usgs.gov", 443), timeout=5).close()
        return True
    except OSError:
        return False


_ONLINE = _network_ok()
_offline_skip = pytest.mark.skipif(not _ONLINE, reason="USGS host unreachable")


def _recent_window() -> tuple[str, str]:
    """A 5-day window ending ~3 days back to avoid publication latency."""
    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=5)
    return start.isoformat(), end.isoformat()


@_offline_skip
def test_daily_discharge_legacy(tmp_path: Path):
    """A daily discharge pull has the right columns, or skips if the window is empty."""
    start, end = _recent_window()
    df = EarthLens(
        data_source="usgs-water",
        start=start,
        end=end,
        variables=["discharge"],
        lat_lim=_DC_LAT,
        lon_lim=_DC_LON,
        path=str(tmp_path),
        service="daily",
        sites=_SITE,
        api="legacy",
    ).download(progress_bar=False)
    if df.empty:
        pytest.skip("USGS returned no daily discharge rows for the window")
    assert {"site_no", "datetime", "value"} <= set(df.columns)
    assert (df["parameter_code"] == "00060").all()


@_offline_skip
def test_statistics_monthly_legacy(tmp_path: Path):
    """A monthly statistics pull over a fixed historical window returns summary rows."""
    df = EarthLens(
        data_source="usgs-water",
        start="2020-01-01",
        end="2021-12-31",
        variables=["discharge"],
        lat_lim=_DC_LAT,
        lon_lim=_DC_LON,
        path=str(tmp_path),
        service="statistics",
        sites=_SITE,
        api="legacy",
        stat_type="monthly",
    ).download(progress_bar=False)
    # A fixed 2020-2021 window at a long-running gauge always has published data,
    # so an empty result is a real regression, not transient emptiness — assert
    # hard (mirroring the emdat / openaq stable-query e2e tests).
    assert not df.empty
    assert "value" in df.columns


@_offline_skip
def test_sites_discovery_legacy(tmp_path: Path):
    """Site discovery over a fixed DC bbox lists at least one long-running station."""
    df = EarthLens(
        data_source="usgs-water",
        start="2023-01-01",
        end="2023-01-05",
        variables=["discharge"],
        lat_lim=_DC_LAT,
        lon_lim=_DC_LON,
        path=str(tmp_path),
        service="sites",
        api="legacy",
    ).download(progress_bar=False)
    # The DC bbox always contains active gauges, so an empty result is a real
    # regression rather than transient emptiness — assert hard (mirroring the
    # iucn known-species stable-query e2e test).
    assert not df.empty
    assert {"site_no", "latitude", "longitude"} <= set(df.columns)


@_offline_skip
@pytest.mark.skipif(
    not _HAVE_TOKEN, reason="set API_USGS_PAT for the modern samples e2e"
)
def test_samples_modern(tmp_path: Path):
    """A water-quality samples pull (modern endpoint) returns result rows."""
    df = EarthLens(
        data_source="usgs-water",
        start="2018-01-01",
        end="2018-12-31",
        variables=["temperature"],
        lat_lim=_DC_LAT,
        lon_lim=_DC_LON,
        path=str(tmp_path),
        service="samples",
        sites=_SITE,
        api="waterdata",
    ).download(progress_bar=False)
    assert {"value", "detection_limit", "characteristic"} <= set(df.columns)
