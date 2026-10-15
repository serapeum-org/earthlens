"""Live end-to-end tests for the NREL NSRDB / WIND Toolkit backend.

Hits the real keyed NREL/NLR Developer Network CSV API, so these are gated on
the `e2e` marker, a network-reachability check, *and* the presence of the
`NREL_API_KEY` + `NREL_EMAIL` credentials in the environment (a default
`pytest` run, or a run without credentials, skips them cleanly). They catch an
`A1` host / endpoint / param / CSV-layout mis-pin that the offline fixture
tests cannot.

Run with (credentials exported):

    pixi run -e dev pytest -m "e2e and nrel"
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.nrel]

#: A well-inside-NSRDB/WTK-coverage point (Denver, Colorado).
_LAT = 39.74
_LON = -105.18


_HAVE_CREDS = bool(os.environ.get("NREL_API_KEY") and os.environ.get("NREL_EMAIL"))
#: Reachability and credentials are separate gates: an unreachable host is an
#: availability skip the masked-lane guards must see as one, while a missing
#: credential is a configuration skip they must not.
_gate = pytest.mark.skipif(not _HAVE_CREDS, reason="NREL_API_KEY / NREL_EMAIL not set")


@_gate
@pytest.mark.requires_host("developer.nlr.gov")
def test_nsrdb_point(tmp_path: Path):
    """A live one-year NSRDB point returns a non-empty hourly solar frame."""
    df = EarthLens(
        data_source="nrel",
        start="2020-01-01",
        end="2020-12-31",
        variables=["ghi", "dni", "dhi"],
        point=(_LAT, _LON),
        path=str(tmp_path),
    ).download(progress_bar=False)
    assert isinstance(df, pd.DataFrame), f"expected a DataFrame, got {type(df)}"
    assert len(df) > 8000, f"expected ~8760 hourly rows, got {len(df)}"
    assert "GHI" in df.columns, list(df.columns)
    assert 0 <= df["GHI"].max() < 1500, "GHI should peak in a plausible W/m2 range"
    assert (df["lat"] == _LAT).all() and (df["lon"] == _LON).all()
    assert df["product"].unique().tolist() == ["nsrdb-psm3"]


@_gate
@pytest.mark.requires_host("developer.nlr.gov")
def test_wind_toolkit_point(tmp_path: Path):
    """A live one-year WIND Toolkit point returns a non-empty hourly wind frame."""
    df = EarthLens(
        data_source="wind-toolkit",
        start="2012-01-01",
        end="2012-12-31",
        variables=["windspeed_100m"],
        point=(_LAT, _LON),
        path=str(tmp_path),
    ).download(progress_bar=False)
    assert len(df) > 8000, f"expected ~8760 hourly rows, got {len(df)}"
    speed_cols = [c for c in df.columns if "wind speed" in c.lower()]
    assert speed_cols, list(df.columns)
    assert df[speed_cols[0]].max() > 0
    assert df["product"].unique().tolist() == ["wtk"]
