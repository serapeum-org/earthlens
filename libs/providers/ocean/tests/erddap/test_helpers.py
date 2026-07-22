"""Unit tests for the pure ERDDAP request helpers."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from earthlens.erddap._helpers import (
    build_constraints,
    build_griddap_url,
    empty_canonical,
)

from earthlens.base import SpatialExtent, TemporalExtent

pytestmark = pytest.mark.erddap


def _space() -> SpatialExtent:
    """A small WGS84 bbox over the equatorial west Pacific."""
    return SpatialExtent.from_pairs(lat_lim=[0.0, 1.0], lon_lim=[150.0, 151.0])


def _time() -> TemporalExtent:
    """A one-day June 2023 window."""
    start = dt.datetime(2023, 6, 1, 12, 0, 0)
    end = dt.datetime(2023, 6, 1, 12, 0, 0)
    return TemporalExtent(
        start_date=start,
        end_date=end,
        resolution="D",
        dates=pd.DatetimeIndex([start]),
    )


def test_build_constraints_tabledap_keys_and_values():
    """tabledap returns the six bbox+time subset keys with right values."""
    c = build_constraints(_space(), _time(), "tabledap")
    assert sorted(c) == [
        "latitude<=",
        "latitude>=",
        "longitude<=",
        "longitude>=",
        "time<=",
        "time>=",
    ]
    assert c["time>="] == "2023-06-01T12:00:00Z"
    assert c["time<="] == "2023-06-01T12:00:00Z"
    assert c["latitude>="] == 0.0
    assert c["latitude<="] == 1.0
    assert c["longitude>="] == 150.0
    assert c["longitude<="] == 151.0


def test_build_constraints_griddap_adds_step_keys():
    """griddap adds a stride of 1 per axis on top of the subset keys."""
    c = build_constraints(_space(), _time(), "griddap")
    assert c["time_step"] == 1
    assert c["latitude_step"] == 1
    assert c["longitude_step"] == 1
    # The shared >=/<= core is identical to tabledap.
    assert c["latitude>="] == 0.0 and c["longitude<="] == 151.0


def test_build_constraints_rejects_unknown_protocol():
    """An unknown protocol fails loud rather than guessing a shape."""
    with pytest.raises(ValueError, match="tabledap.*griddap"):
        build_constraints(_space(), _time(), "wmsdap")


def test_build_griddap_url_full_cube():
    """A time/lat/lon cube subsets every axis in dim order."""
    constraints = build_constraints(_space(), _time(), "griddap")
    url = build_griddap_url(
        "https://example.org/erddap",
        "NOAA_DHW",
        ["CRW_SSTANOMALY"],
        ["time", "latitude", "longitude"],
        constraints,
    )
    assert url == (
        "https://example.org/erddap/griddap/NOAA_DHW.nc?CRW_SSTANOMALY"
        "[(2023-06-01T12:00:00Z):1:(2023-06-01T12:00:00Z)]"
        "[(0.0):1:(1.0)][(150.0):1:(151.0)]"
    )


def test_build_griddap_url_strips_trailing_slash_and_joins_variables():
    """A trailing slash is tolerated and multiple variables join with commas."""
    constraints = build_constraints(_space(), _time(), "griddap")
    url = build_griddap_url(
        "https://example.org/erddap/",
        "ds",
        ["a", "b"],
        ["time", "latitude", "longitude"],
        constraints,
    )
    assert url.startswith("https://example.org/erddap/griddap/ds.nc?a[")
    assert ",b[" in url


def test_build_griddap_url_unconstrained_dim_is_full_range():
    """A dimension with no constraint becomes the `[]` full-range selector."""
    constraints = build_constraints(_space(), _time(), "griddap")
    url = build_griddap_url(
        "https://example.org/erddap",
        "ds",
        ["v"],
        ["time", "altitude", "latitude", "longitude"],
        constraints,
    )
    assert "[]" in url  # the altitude axis is unconstrained


def test_empty_canonical_columns_and_zero_rows():
    """empty_canonical yields exactly the requested columns and no rows."""
    df = empty_canonical(["time", "sst"])
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["time", "sst"]
    assert len(df) == 0
