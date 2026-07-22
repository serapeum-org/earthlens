"""Live end-to-end tests for the climate-indices backend.

Hits the real NOAA PSL and KNMI Climate Explorer hosts. Both are open
(no credentials), so these are gated only on the `e2e` marker plus a
quick network-reachability skip (a default `pytest` run skips them).

Run with:

    pixi run -e dev pytest -m "e2e and climate_indices"
"""

from __future__ import annotations

import socket
from pathlib import Path

import pandas as pd
import pytest
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.climate_indices]


def _host_ok(host: str) -> bool:
    """Return True when `host` is reachable on port 443."""
    try:
        socket.create_connection((host, 443), timeout=5).close()
        return True
    except OSError:
        return False


_psl_skip = pytest.mark.skipif(
    not _host_ok("psl.noaa.gov"), reason="NOAA PSL host unreachable"
)
_climexp_skip = pytest.mark.skipif(
    not _host_ok("climexp.knmi.nl"), reason="KNMI climexp host unreachable"
)


@_psl_skip
def test_oni_psl_live(tmp_path: Path) -> None:
    """A live ONI pull (NOAA PSL, psl dialect) returns plausible monthly rows."""
    df = EarthLens(
        data_source="climate-indices",
        variables=["oni"],
        start="1990-01-01",
        end="2000-12-31",
        path=str(tmp_path),
    ).download()
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["date", "index", "value", "source"]
    assert set(df["index"].unique()) == {"oni"}
    assert (df["source"] == "noaa-psl").all()
    assert len(df) == 12 * 11
    values = df["value"].dropna()
    assert not values.empty
    assert values.between(-4.0, 4.0).all()


@_climexp_skip
def test_amo_climexp_live(tmp_path: Path) -> None:
    """A live AMO pull (KNMI climexp, climexp dialect) returns plausible rows."""
    df = EarthLens(
        data_source="climate-indices",
        variables=["amo"],
        start="1990-01-01",
        end="2000-12-31",
        path=str(tmp_path),
    ).download()
    assert list(df.columns) == ["date", "index", "value", "source"]
    assert set(df["index"].unique()) == {"amo"}
    assert (df["source"] == "knmi-climexp").all()
    values = df["value"].dropna()
    assert not values.empty
    assert values.between(-2.0, 2.0).all()
