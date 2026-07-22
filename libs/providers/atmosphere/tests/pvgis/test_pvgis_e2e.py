"""Live end-to-end tests for the JRC PVGIS backend.

Hits the real keyless PVGIS 5.3 REST API (no credentials), so these are
gated only on the `e2e` marker plus a quick network-reachability skip (a
default `pytest` run skips them). They catch an `A1` endpoint / param
mis-pin that the offline fixture tests cannot.

Run with:

    pixi run -e dev pytest -m "e2e and pvgis"
"""

from __future__ import annotations

import socket
from pathlib import Path

import pandas as pd
import pytest
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.pvgis]

#: A well-inside-coverage point (northern Italy) PVGIS serves from SARAH-3.
_LAT = 45.0
_LON = 8.0


def _network_ok() -> bool:
    """Return True when the PVGIS host is reachable on 443."""
    try:
        socket.create_connection(("re.jrc.ec.europa.eu", 443), timeout=5).close()
        return True
    except OSError:
        return False


_ONLINE = _network_ok()
_offline_skip = pytest.mark.skipif(not _ONLINE, reason="PVGIS host unreachable")


@_offline_skip
def test_seriescalc_point(tmp_path: Path):
    """A live one-year seriescalc point returns a non-empty hourly frame."""
    df = EarthLens(
        data_source="pvgis",
        start="2020-01-01",
        end="2020-12-31",
        variables=["seriescalc"],
        point=(_LAT, _LON),
        path=str(tmp_path),
    ).download(progress_bar=False)
    assert isinstance(df, pd.DataFrame), f"expected a DataFrame, got {type(df)}"
    assert len(df) > 8000, f"expected ~8760 hourly rows, got {len(df)}"
    assert "G(i)" in df.columns, list(df.columns)
    assert df["G(i)"].max() > 100, "in-plane irradiance should peak well above 100 W/m2"
    assert (df["lat"] == _LAT).all() and (df["lon"] == _LON).all(), "lat/lon tags wrong"


@_offline_skip
def test_tmy_point(tmp_path: Path):
    """A live tmy point returns the 8760-hour TMY frame with humidity."""
    df = EarthLens(
        data_source="pvgis",
        start="2020-01-01",
        end="2020-12-31",
        variables=["tmy"],
        point=(_LAT, _LON),
        path=str(tmp_path),
    ).download(progress_bar=False)
    assert len(df) == 8760, f"expected 8760 TMY hours, got {len(df)}"
    assert {"RH", "G(h)", "T2m"}.issubset(df.columns), list(df.columns)
