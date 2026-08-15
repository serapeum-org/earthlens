"""Unit tests for the pure ERDDAP request helpers."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.erddap._helpers import (
    build_constraints,
    build_griddap_url,
    empty_canonical,
)

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


def _space_sf() -> SpatialExtent:
    """A San-Francisco-Bay bbox with negative (−180..180) longitudes."""
    return SpatialExtent.from_pairs(lat_lim=[37.0, 38.5], lon_lim=[-123.5, -121.5])


def test_build_constraints_lon_360_shifts_negative_longitudes():
    """`lon_360` maps a −180..180 tabledap box into the 0..360 convention."""
    c = build_constraints(_space_sf(), _time(), "tabledap", lon_360=True)
    assert c["longitude>="] == pytest.approx(236.5)
    assert c["longitude<="] == pytest.approx(238.5)
    # Latitude/time keys are untouched by the shift.
    assert c["latitude>="] == 37.0
    assert c["latitude<="] == 38.5


def test_build_constraints_lon_360_ignored_when_false():
    """Without the flag the negative longitudes pass through unchanged."""
    c = build_constraints(_space_sf(), _time(), "tabledap")
    assert c["longitude>="] == -123.5
    assert c["longitude<="] == -121.5


def test_build_constraints_lon_360_global_box_drops_longitude_keys():
    """A near-global `lon_360` box drops the un-expressible longitude filter."""
    world = SpatialExtent.from_pairs(lat_lim=[-80.0, 80.0], lon_lim=[-180.0, 180.0])
    c = build_constraints(world, _time(), "tabledap", lon_360=True)
    assert "longitude>=" not in c
    assert "longitude<=" not in c
    # Latitude + time still subset the stations.
    assert c["latitude>="] == -80.0
    assert c["time>="] == "2023-06-01T12:00:00Z"


def test_build_constraints_lon_360_greenwich_box_drops_longitude_keys():
    """A `lon_360` box straddling 0 deg drops the longitude keys and warns."""
    from loguru import logger

    channel = SpatialExtent.from_pairs(lat_lim=[49.0, 52.0], lon_lim=[-1.0, 1.0])
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        c = build_constraints(channel, _time(), "tabledap", lon_360=True)
    finally:
        logger.remove(sink)
    assert "longitude>=" not in c
    assert "longitude<=" not in c
    # Latitude still constrains, and the drop is not silent.
    assert c["latitude>="] == 49.0
    assert any("wraps the 0/360 seam" in message for message in messages)


def test_build_constraints_lon_360_no_effect_on_griddap():
    """`lon_360` never touches griddap constraints (grids carry their axis)."""
    c = build_constraints(_space_sf(), _time(), "griddap", lon_360=True)
    assert c["longitude>="] == -123.5
    assert c["longitude<="] == -121.5


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
