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


_offline_skip = pytest.mark.skipif(not _network_ok(), reason=f"{_HOST} unreachable")


#: Every shipped griddap row paired with a window inside its real coverage and
#: a variable, so each catalog row is fetch-verified (not just metadata-probed).
#: erdMH1chla8day is a historical MODIS product (2003-2022), hence the 2020
#: window; erdPH53sstd8day ends 2023-06, hence the early-June date. Only
#: NOAA_DHW is still updating.
_GRIDDAP_CASES = [
    ("NOAA_DHW", "CRW_SSTANOMALY", "2023-06-01", "2023-06-01"),
    ("erdPH53sstd8day", "sea_surface_temperature", "2023-06-01", "2023-06-01"),
    ("erdMH1chla8day", "chlorophyll", "2020-06-01", "2020-06-10"),
]


@_offline_skip
@pytest.mark.parametrize("dataset, variable, start, end", _GRIDDAP_CASES)
def test_griddap_fetches_readable_netcdf(tmp_path, dataset, variable, start, end):
    """Every shipped griddap dataset writes a pyramids-readable NetCDF in-coverage."""
    result = EarthLens(
        data_source="erddap",
        dataset=dataset,
        variables=[variable],
        start=start,
        end=end,
        lat_lim=[0.0, 1.0],
        lon_lim=[150.0, 151.0],
        path=str(tmp_path),
    ).download()

    assert isinstance(result, list) and result
    nc_path = result[0]
    assert nc_path.is_file() and nc_path.suffix == ".nc"

    from pyramids.netcdf import NetCDF

    assert NetCDF.read_file(str(nc_path)) is not None


@_offline_skip
def test_griddap_out_of_coverage_raises(tmp_path: Path):
    """A request past a dataset's coverage raises a clear out-of-coverage error."""
    with pytest.raises(ValueError, match="outside the dataset's coverage"):
        EarthLens(
            data_source="erddap",
            dataset="erdMH1chla8day",  # historical: data ends 2022-06
            variables=["chlorophyll"],
            start="2025-06-01",
            end="2025-06-10",
            lat_lim=[0.0, 1.0],
            lon_lim=[150.0, 151.0],
            path=str(tmp_path),
        ).download()


@_offline_skip
def test_tabledap_ndbc_buoys(tmp_path: Path):
    """A small NDBC buoy tabledap pull returns a pandas DataFrame."""
    df = EarthLens(
        data_source="erddap",
        dataset="cwwcNDBCMet",
        variables=["station", "time", "WTMP"],
        start="2023-01-01",
        end="2023-01-01",
        lat_lim=[36.0, 37.0],
        lon_lim=[-123.0, -122.0],
        path=str(tmp_path),
    ).download()

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert any("station" in str(col) for col in df.columns)
