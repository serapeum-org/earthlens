from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from earthlens.base import (
    AbstractDataSource,
    SpatialExtent,
    TemporalExtent,
    to_datetime,
)


class _Windowed(AbstractDataSource):
    """Minimal backend that needs both bounds (the inherited default)."""

    def _initialize(self):
        return None

    def _create_grid(self, lat_lim, lon_lim):
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def download(self, progress_bar: bool = True, **kwargs):
        return []

    def _api(self):
        return []


class _Snapshot(_Windowed):
    """Backend with no time axis, so a missing bound is legal."""

    REQUIRES_TIME_WINDOW = False

    def _check_input_dates(self, start, end, temporal_resolution, fmt):
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution="static",
            dates=pd.DatetimeIndex([]),
        )


def _build(cls, tmp_path, **kwargs):
    kwargs.setdefault("variables", ["x"])
    kwargs.setdefault("lat_lim", [0.0, 1.0])
    kwargs.setdefault("lon_lim", [0.0, 1.0])
    return cls(path=str(tmp_path), **kwargs)


class TestCheckTimeWindow:
    """Up-front rejection of a missing start / end bound."""

    def test_both_bounds_present_constructs(self, tmp_path):
        """A complete window passes the guard and builds the backend."""
        backend = _build(_Windowed, tmp_path, start="2024-01-01", end="2024-01-02")
        assert backend.time.start_date == dt.datetime(2024, 1, 1)

    @pytest.mark.parametrize(
        "kwargs,named",
        [
            ({"start": None, "end": "2024-01-02"}, "start"),
            ({"start": "2024-01-01", "end": None}, "end"),
        ],
    )
    def test_single_missing_bound_names_it(self, tmp_path, kwargs, named):
        """The error names exactly which bound is missing."""
        with pytest.raises(ValueError, match=f"requires a time window, but {named} is"):
            _build(_Windowed, tmp_path, **kwargs)

    def test_both_missing_names_both(self, tmp_path):
        """With neither bound given the error names both, pluralised."""
        with pytest.raises(ValueError, match="start and end are missing"):
            _build(_Windowed, tmp_path, start=None, end=None)

    def test_error_names_the_backend_class(self, tmp_path):
        """The message identifies the backend so the user knows what to fix."""
        with pytest.raises(ValueError, match="the _Windowed backend requires"):
            _build(_Windowed, tmp_path, start=None, end=None)

    def test_error_suggests_both_call_shapes(self, tmp_path):
        """The remedy mentions start=/end= and the single time= range."""
        with pytest.raises(ValueError) as excinfo:
            _build(_Windowed, tmp_path, start=None, end=None)
        message = str(excinfo.value)
        assert "start=/end=" in message
        assert "time=" in message

    def test_guard_runs_before_date_parsing(self, tmp_path):
        """The guard raises ValueError, not the bare TypeError strptime would give."""
        with pytest.raises(ValueError):
            _build(_Windowed, tmp_path, start=None, end=None)

    def test_opt_out_backend_accepts_missing_bounds(self, tmp_path):
        """REQUIRES_TIME_WINDOW = False lets both bounds stay None."""
        backend = _build(_Snapshot, tmp_path, start=None, end=None)
        assert backend.time.start_date is None

    def test_opt_out_backend_still_accepts_a_window(self, tmp_path):
        """Opting out does not forbid passing dates."""
        backend = _build(_Snapshot, tmp_path, start="2024-01-01", end="2024-01-02")
        assert backend.time.resolution == "static"

    def test_default_is_required(self):
        """The base class requires a window unless a backend opts out."""
        assert AbstractDataSource.REQUIRES_TIME_WINDOW is True

    def test_guard_is_callable_without_an_instance(self):
        """The guard is a classmethod, so it validates before construction."""
        _Windowed._check_time_window("2024-01-01", "2024-01-02")
        with pytest.raises(ValueError, match="requires a time window"):
            _Windowed._check_time_window(None, None)

    def test_opt_out_guard_is_a_noop(self):
        """An opted-out backend's guard accepts anything, including None."""
        assert _Snapshot._check_time_window(None, None) is None
