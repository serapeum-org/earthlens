"""Shared offline fixtures for the USGS Water backend tests.

Drives the backend end-to-end without network by replacing the
`dataretrieval.waterdata` / `dataretrieval.nwis` submodules in
`sys.modules` with recording fakes. Each fake function records its
call kwargs and returns a configured `(DataFrame, metadata)` tuple (or
raises a 429-like error), so tests assert on dispatch, query-kwarg
shaping, and the modern/legacy fallbacks without any network.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Callable

import pandas as pd
import pytest


def modern_long_frame(
    *,
    site: str = "01646500",
    code: str = "00060",
    unit: str = "ft^3/s",
    statistic_id: str = "00003",
    n: int = 3,
) -> pd.DataFrame:
    """Build a modern `waterdata` long/tidy values frame."""
    times = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "monitoring_location_id": [f"USGS-{site}"] * n,
            "parameter_code": [code] * n,
            "statistic_id": [statistic_id] * n,
            "time": times,
            "value": [100 + i for i in range(n)],
            "unit_of_measure": [unit] * n,
            "qualifier": [None] * n,
        }
    )


def legacy_wide_frame(
    *,
    site: str = "01646500",
    code: str = "00060",
    stat: str = "Mean",
    n: int = 3,
) -> pd.DataFrame:
    """Build a legacy `nwis` wide values frame (datetime index)."""
    idx = pd.to_datetime(pd.date_range("2023-01-01", periods=n, freq="D"), utc=True)
    idx.name = "datetime"
    column = f"{code}_{stat}" if stat else code
    return pd.DataFrame(
        {
            column: [100 + i for i in range(n)],
            f"{column}_cd": ["A"] * n,
            "site_no": [site] * n,
        },
        index=idx,
    )


class _RateLimited(Exception):
    """Stand-in for the SDK's HTTP 429 quota-exhausted error."""


class FakeUSGS:
    """Recording state for the faked `dataretrieval` submodules.

    Holds the `(frame, raises)` each named function should return and a
    log of every call as `(flavour, fn_name, kwargs)`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._returns: dict[str, pd.DataFrame] = {}
        self._raises: dict[str, BaseException] = {}

    def set_return(self, fn_name: str, frame: pd.DataFrame) -> None:
        """Make the named SDK function return `(frame, {})`."""
        self._returns[fn_name] = frame

    def set_raise(self, fn_name: str, exc: BaseException) -> None:
        """Make the named SDK function raise `exc`."""
        self._raises[fn_name] = exc

    def rate_limit(self, fn_name: str) -> None:
        """Make the named SDK function raise a 429-like error."""
        self._raises[fn_name] = _RateLimited("HTTP 429: Too many requests")

    def _make(self, flavour: str, fn_name: str) -> Callable[..., Any]:
        def fn(**kwargs: Any) -> Any:
            self.calls.append((flavour, fn_name, kwargs))
            if fn_name in self._raises:
                raise self._raises[fn_name]
            frame = self._returns.get(fn_name)
            if frame is None:
                frame = (
                    modern_long_frame()
                    if flavour == "waterdata"
                    else (legacy_wide_frame())
                )
            return frame, {"meta": True}

        return fn

    def called(self) -> list[str]:
        """Return the ordered list of called function names."""
        return [name for _flavour, name, _kw in self.calls]

    def kwargs_for(self, fn_name: str) -> dict[str, Any]:
        """Return the kwargs of the first recorded call to `fn_name`."""
        for _flavour, name, kwargs in self.calls:
            if name == fn_name:
                return kwargs
        raise KeyError(fn_name)


_WATERDATA_FNS = [
    "get_daily",
    "get_continuous",
    "get_samples",
    "get_stats_date_range",
    "get_stats_por",
    "get_field_measurements",
    "get_peaks",
    "get_ratings",
    "get_monitoring_locations",
    "get_reference_table",
]
_NWIS_FNS = [
    "get_dv",
    "get_iv",
    "get_qwdata",
    "get_stats",
    "get_discharge_measurements",
    "get_discharge_peaks",
    "get_ratings",
    "what_sites",
    "get_info",
]


@pytest.fixture
def fake_usgs(monkeypatch: pytest.MonkeyPatch) -> FakeUSGS:
    """Replace the `dataretrieval` submodules with recording fakes."""
    state = FakeUSGS()
    root = types.ModuleType("dataretrieval")
    root.__version__ = "fake"
    waterdata = types.ModuleType("dataretrieval.waterdata")
    nwis = types.ModuleType("dataretrieval.nwis")
    for name in _WATERDATA_FNS:
        setattr(waterdata, name, state._make("waterdata", name))
    for name in _NWIS_FNS:
        setattr(nwis, name, state._make("nwis", name))
    monkeypatch.setitem(sys.modules, "dataretrieval", root)
    monkeypatch.setitem(sys.modules, "dataretrieval.waterdata", waterdata)
    monkeypatch.setitem(sys.modules, "dataretrieval.nwis", nwis)
    return state


@pytest.fixture
def usgs_kwargs(tmp_path) -> Callable[..., dict[str, Any]]:
    """Factory for the standard `USGSWater(...)` constructor kwargs."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "start": "2023-01-01",
            "end": "2023-01-05",
            "variables": ["discharge"],
            "lat_lim": [38.9, 39.0],
            "lon_lim": [-77.2, -77.0],
            "path": str(tmp_path),
        }
        base.update(overrides)
        return base

    return _make
