"""Live end-to-end tests for the risk-indicators backend.

Hits the real ThinkHazard! and INFORM (JRC) hosts — both public, gated only on
the `e2e` marker plus a network-reachability skip — and the Global Forest Watch
Data API, additionally gated on a `GFW_API_KEY` in the environment. A default
`pytest` run skips them all.

INFORM is read through two channels and both are exercised here: the published
release workbook (the default — a results-page fetch plus a ~2.5 MB download,
cached per test under `tmp_path`) and the Scores API.

The JRC host drops connections without a response after roughly two API requests
in quick succession, and this module makes several contacts with it — the
reachability probe at import, the results page, the workbook, then the API pulls.
`HttpClient` retries a dropped connection at 1s and 2s, which covers it in
practice; a drop that outlasts that budget surfaces as a transport error, which
the shared `pytest_runtest_call` hook classifies as an availability skip rather
than a failure. If it ever does turn flaky, merge the INFORM tests into one pull
per channel.

Run with:

    uv run pytest -m "e2e and risk_indicators"
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.risk_indicators]


def _host_ok(host: str) -> bool:
    """Return True when `host` is reachable on port 443."""
    try:
        socket.create_connection((host, 443), timeout=5).close()
        return True
    except OSError:
        return False


_thinkhazard_skip = pytest.mark.skipif(
    not _host_ok("thinkhazard.org"), reason="ThinkHazard host unreachable"
)
_inform_skip = pytest.mark.skipif(
    not _host_ok("drmkc.jrc.ec.europa.eu"), reason="INFORM host unreachable"
)
_gfw_skip = pytest.mark.skipif(
    not (os.environ.get("GFW_API_KEY") and _host_ok("data-api.globalforestwatch.org")),
    reason="GFW_API_KEY unset or GFW host unreachable",
)


@_thinkhazard_skip
def test_thinkhazard_live(tmp_path: Path) -> None:
    """A live ThinkHazard pull for Kenya returns a river-flood hazard level."""
    df = EarthLens(
        data_source="risk-indicators",
        variables=["thinkhazard:flood_river"],
        country="KEN",
        path=str(tmp_path),
    ).download()
    assert isinstance(df, pd.DataFrame)
    assert df.iloc[0]["hazard"] == "FL"
    assert df.iloc[0]["level"] in {"VLO", "LOW", "MED", "HIG"}


@_thinkhazard_skip
def test_thinkhazard_all_live(tmp_path: Path) -> None:
    """A live all-hazards ThinkHazard pull returns the 11 hazards."""
    df = EarthLens(
        data_source="thinkhazard",
        variables=["thinkhazard:all"],
        country="KEN",
        path=str(tmp_path),
    ).download()
    assert len(df) == 11


@_inform_skip
def test_inform_live(tmp_path: Path) -> None:
    """A live INFORM pull for Kenya reads the current published release."""
    df = EarthLens(
        data_source="inform",
        variables=["inform:risk"],
        country="KEN",
        path=str(tmp_path),
        cache_dir=str(tmp_path / "cache"),
    ).download()
    assert len(df) == 1
    assert df.iloc[0]["iso3"] == "KEN"
    assert 0.0 <= float(df.iloc[0]["indicator_score"]) <= 10.0
    # The default reads the workbook JRC publishes, so the row must be tagged as
    # a release row and carry a release year - the API leaves that at 0.
    assert df.iloc[0]["source"] == "release"
    assert int(df.iloc[0]["validity_year"]) >= 2026


@_inform_skip
def test_inform_api_live(tmp_path: Path) -> None:
    """The API channel still answers, from the workflow the catalog pins."""
    df = EarthLens(
        data_source="inform",
        variables=["inform:risk"],
        country="KEN",
        source="api",
        path=str(tmp_path),
    ).download()
    assert len(df) == 1
    assert df.iloc[0]["iso3"] == "KEN"
    assert 0.0 <= float(df.iloc[0]["indicator_score"]) <= 10.0
    assert int(df.iloc[0]["workflow_id"]) == 503
    assert df.iloc[0]["source"] == "api"


@_inform_skip
def test_inform_climate_risk_live(tmp_path: Path) -> None:
    """A live INFORM climate-change pull for Kenya returns the 2050 projection."""
    df = EarthLens(
        data_source="inform",
        variables=["inform:climate_risk"],
        country="KEN",
        path=str(tmp_path),
    ).download()
    assert len(df) == 1
    assert df.iloc[0]["iso3"] == "KEN"
    assert 0.0 <= float(df.iloc[0]["indicator_score"]) <= 10.0
    # The climate projection is a separate, static 2022-era model, so the row
    # must come from workflow 451 - not from whichever Risk workflow is pinned.
    assert int(df.iloc[0]["workflow_id"]) == 451


@_gfw_skip
def test_gfw_tree_cover_loss_live(tmp_path: Path) -> None:
    """A live GFW tree-cover-loss query for Kenya returns per-year loss rows."""
    df = EarthLens(
        data_source="gfw",
        variables=["gfw:tree_cover_loss"],
        country="KEN",
        path=str(tmp_path),
    ).download()
    assert isinstance(df, pd.DataFrame) and not df.empty
    assert "umd_tree_cover_loss__year" in df.columns


@_gfw_skip
def test_gfw_admin_boundary_live(tmp_path: Path) -> None:
    """A live GFW geostore pull for Kenya returns an admin-boundary FeatureCollection."""
    result = EarthLens(
        data_source="gfw",
        variables=["gfw:admin_boundary"],
        country="KEN",
        path=str(tmp_path),
    ).download()
    assert isinstance(result, FeatureCollection) and len(result) >= 1
