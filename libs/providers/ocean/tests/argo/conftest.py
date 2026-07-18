"""Shared offline fixtures for the Argo backend tests.

Drives the backend end-to-end without network by replacing the `argopy`
module in `sys.modules` with a recording fake. The fake `DataFetcher`
records its constructor kwargs and the selection method called
(`.region` / `.float` / `.profile`), and its `.to_dataframe()` returns a
fixture frame — so tests assert on the construction knobs, the selection
dispatch, and the pandas realise path without any network. The fake has
**no** `.to_xarray()`, so any accidental use surfaces as `AttributeError`.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import pandas as pd
import pytest


def phy_frame(n: int = 3) -> pd.DataFrame:
    """Build a small phy profile frame with an N_POINTS index, like argopy."""
    idx = pd.RangeIndex(n, name="N_POINTS")
    return pd.DataFrame(
        {
            "PLATFORM_NUMBER": [6902746] * n,
            "CYCLE_NUMBER": [12] * n,
            "DIRECTION": ["A"] * n,
            "DATA_MODE": ["D"] * n,
            "TIME": pd.date_range("2020-01-03", periods=n, freq="h"),
            "LATITUDE": [43.3] * n,
            "LONGITUDE": [-58.1] * n,
            "PRES": [1.7 + i for i in range(n)],
            "TEMP": [12.7 - i * 0.1 for i in range(n)],
            "PSAL": [34.6 + i * 0.01 for i in range(n)],
        },
        index=idx,
    )


@dataclass
class FakeArgo:
    """Recording state for the faked `argopy` module.

    Holds the frame `.to_dataframe()` should return (or an exception to
    raise) and a log of the constructor kwargs and selection calls.
    """

    frame: pd.DataFrame = field(default_factory=phy_frame)
    raises: BaseException | None = None
    ctor_kwargs: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def method(self) -> str | None:
        """Return the name of the selection method that was called."""
        return self.calls[-1][0] if self.calls else None

    def call_args(self, name: str) -> Any:
        """Return the argument of the first recorded call to `name`."""
        for called, args in self.calls:
            if called == name:
                return args
        raise KeyError(name)


class _Fetcher:
    """Fake `argopy.DataFetcher` — records selection calls, no `.to_xarray`."""

    def __init__(self, state: FakeArgo) -> None:
        self._state = state

    def region(self, box: list) -> _Fetcher:
        """Record a `.region(box)` call and return self for chaining."""
        self._state.calls.append(("region", box))
        return self

    def float(self, wmos: list[int]) -> _Fetcher:
        """Record a `.float(wmos)` call and return self for chaining."""
        self._state.calls.append(("float", wmos))
        return self

    def profile(self, wmo: int, cyc: int) -> _Fetcher:
        """Record a `.profile(wmo, cyc)` call and return self for chaining."""
        self._state.calls.append(("profile", (wmo, cyc)))
        return self

    def to_dataframe(self) -> pd.DataFrame:
        """Return the configured frame, or raise the configured error."""
        if self._state.raises is not None:
            raise self._state.raises
        return self._state.frame


class DataNotFound(ValueError):
    """Stand-in for `argopy.errors.DataNotFound` (a ValueError subclass)."""


class NoData(ValueError):
    """Stand-in for `argopy.errors.NoData`."""


class ErddapHTTPNotFound(Exception):
    """Stand-in for `argopy.errors.ErddapHTTPNotFound`."""


@pytest.fixture
def fake_argopy(monkeypatch: pytest.MonkeyPatch) -> FakeArgo:
    """Replace the `argopy` module with a recording fake."""
    state = FakeArgo()

    def make_fetcher(**kwargs: Any) -> _Fetcher:
        state.ctor_kwargs = kwargs
        return _Fetcher(state)

    module = types.ModuleType("argopy")
    module.__version__ = "fake"
    module.DataFetcher = make_fetcher
    errors = types.ModuleType("argopy.errors")
    errors.DataNotFound = DataNotFound
    errors.NoData = NoData
    errors.ErddapHTTPNotFound = ErddapHTTPNotFound
    module.errors = errors
    monkeypatch.setitem(sys.modules, "argopy", module)
    monkeypatch.setitem(sys.modules, "argopy.errors", errors)
    return state


@pytest.fixture
def argo_kwargs(tmp_path) -> Callable[..., dict[str, Any]]:
    """Factory for the standard `ARGO(...)` constructor kwargs."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "start": "2020-01-01",
            "end": "2020-01-15",
            "variables": ["TEMP", "PSAL"],
            "lat_lim": [40.0, 45.0],
            "lon_lim": [-60.0, -55.0],
            "path": str(tmp_path),
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def info_log() -> Iterator[list[str]]:
    """Capture INFO-and-above loguru messages into a list for the test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
