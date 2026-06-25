"""Live end-to-end tests for the generic ERDDAP backend.

Hits the real NOAA CoastWatch ERDDAP (public, no auth), so these are
gated on the `e2e` + `erddap` markers plus a quick network-reachability
skip (a default `pytest` run skips them). They prove the two realisation
paths the A1 gate pinned actually work against a live server: a griddap
`.nc` download readable by pyramids, and a tabledap `to_pandas()` frame.

Run with:

    pixi run -e dev pytest -m "e2e and erddap"
"""

from __future__ import annotations

import socket
from pathlib import Path

import pandas as pd
import pytest

from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.e2e, pytest.mark.erddap]

_HOST = "coastwatch.pfeg.noaa.gov"


def _network_ok() -> bool:
    """Return True when the CoastWatch ERDDAP host is reachable on 443."""
    try:
        socket.create_connection((_HOST, 443), timeout=5).close()
        return True
    except OSError:
        return False


_offline_skip = pytest.mark.skipif(
    not _network_ok(), reason=f"{_HOST} unreachable"
)


@_offline_skip
def test_griddap_crw_sst_anomaly(tmp_path: Path):
    """A tiny CRW SST-anomaly griddap pull writes a pyramids-readable NetCDF."""
    result = EarthLens(
        data_source="erddap",
        dataset="NOAA_DHW",
        variables=["CRW_SSTANOMALY"],
        start="2023-06-01",
        end="2023-06-01",
        lat_lim=[0.0, 1.0],
        lon_lim=[150.0, 151.0],
        path=str(tmp_path),
    ).download()

    assert isinstance(result, list) and result
    nc_path = result[0]
    assert nc_path.is_file() and nc_path.suffix == ".nc"

    from pyramids.netcdf import NetCDF

    dataset = NetCDF.read_file(str(nc_path))
    assert dataset is not None


@_offline_skip
def test_tabledap_ndbc_buoys(tmp_path: Path):
    """A small NDBC buoy tabledap pull returns a pandas DataFrame."""
    df = EarthLens(
        data_source="erddap",
        dataset="cwwcNDBCMet",
        variables=["station", "time", "wtmp"],
        start="2023-01-01",
        end="2023-01-01",
        lat_lim=[36.0, 37.0],
        lon_lim=[-123.0, -122.0],
        path=str(tmp_path),
    ).download()

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert any("station" in str(col) for col in df.columns)
