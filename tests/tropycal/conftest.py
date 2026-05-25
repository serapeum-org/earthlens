"""Shared fixtures for the Tropycal backend tests.

Builds a fake `tropycal` SDK (no network) and injects it into
`sys.modules` so the backend's lazy `import tropycal.tracks` resolves to
the fake. The fake `TrackDataset` records every construction as a
`(basin, source)` tuple so the per-process memo (G3) can be asserted, and
serves hand-built per-storm DataFrames shaped like
`Storm.to_dataframe(attrs_as_columns=True)`.
"""

from __future__ import annotations

import datetime as dt
import types
from collections.abc import Iterator

import pandas as pd
import pytest


def _make_storm_frame(
    *,
    storm_id: str = "AL122005",
    name: str = "KATRINA",
    basin: str = "north_atlantic",
    times: list[str] | None = None,
    lats: list[float] | None = None,
    lons: list[float] | None = None,
    vmax: list[float] | None = None,
    mslp: list[float] | None = None,
    types_: list[str] | None = None,
    ace: float = 20.0,
    attrs_as_columns: bool = True,
) -> pd.DataFrame:
    """Build one storm's per-fix DataFrame (the tropycal to_dataframe shape).

    Defaults to a three-fix Katrina-like Atlantic track. `attrs_as_columns`
    mirrors tropycal: when False the `id`/`name`/`ace` columns are omitted.
    """
    times = times or ["2005-08-25 00:00", "2005-08-25 06:00", "2005-08-28 12:00"]
    lats = lats or [25.4, 25.9, 26.5]
    lons = lons or [-80.3, -81.0, -86.0]
    vmax = vmax or [45.0, 80.0, 150.0]
    mslp = mslp or [997.0, 985.0, 902.0]
    types_ = types_ or ["TS", "HU", "HU"]
    data: dict[str, object] = {
        "time": pd.to_datetime(times),
        "lat": lats,
        "lon": lons,
        "vmax": vmax,
        "mslp": mslp,
        "type": types_,
        "wmo_basin": [basin] * len(times),
    }
    if attrs_as_columns:
        data["id"] = [storm_id] * len(times)
        data["name"] = [name] * len(times)
        data["ace"] = [ace] * len(times)
    return pd.DataFrame(data)


class _FakeShips:
    """Stand-in for a tropycal `Ships` object exposing `to_dataframe`."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def to_dataframe(self) -> pd.DataFrame:
        return self._frame


class _FakeStorm:
    """Stand-in for a tropycal `Storm` returning canned DataFrames."""

    def __init__(self, frame: pd.DataFrame, ships_frame: pd.DataFrame | None = None) -> None:
        self._frame = frame
        self._ships_frame = ships_frame

    def to_dataframe(self, attrs_as_columns: bool = False) -> pd.DataFrame:
        return self._frame

    def get_ships(self, time) -> _FakeShips:
        if self._ships_frame is None:
            raise ValueError("SHIPS data is unavailable for the requested storm or time.")
        return _FakeShips(self._ships_frame)


class _FakeSeason:
    """Stand-in for a tropycal `Season` exposing a `summary()` id list."""

    def __init__(self, storm_ids: list[str]) -> None:
        self._storm_ids = storm_ids

    def summary(self) -> dict[str, list[str]]:
        return {"id": list(self._storm_ids)}


class _FakeState:
    """Mutable backing store the fake `TrackDataset` reads from.

    `constructions` records one `(basin, source)` per `TrackDataset(...)`
    so the G3 memo can be asserted. `seasons` maps a year to its storm-id
    list; `storms` maps a storm id to its DataFrame.
    """

    def __init__(self) -> None:
        self.constructions: list[tuple[str, str]] = []
        self.seasons: dict[int, list[str]] = {}
        self.storms: dict[str, pd.DataFrame] = {}
        self.ships_frame: pd.DataFrame | None = None
        self.recon_frame: pd.DataFrame | None = None
        self.active_ids: list[str] = []

    def add_storm(self, year: int, frame: pd.DataFrame, storm_id: str | None = None) -> None:
        """Register a storm DataFrame under a season year."""
        storm_id = storm_id or str(frame["id"].iloc[0])
        self.seasons.setdefault(year, []).append(storm_id)
        self.storms[storm_id] = frame

    @property
    def construction_count(self) -> int:
        """Number of `TrackDataset` constructions so far."""
        return len(self.constructions)


def _make_trackdataset_cls(state: _FakeState) -> type:
    """Build a fake `TrackDataset` class bound to `state`."""

    class _FakeTrackDataset:
        def __init__(self, basin: str = "north_atlantic", source: str = "hurdat", **kwargs):
            state.constructions.append((basin, source))

        def get_season(self, year: int) -> _FakeSeason:
            return _FakeSeason(state.seasons.get(year, []))

        def get_storm(self, storm_id: str) -> _FakeStorm:
            return _FakeStorm(state.storms[storm_id], ships_frame=state.ships_frame)

    return _FakeTrackDataset


@pytest.fixture
def make_storm_frame():
    """Factory for one storm's per-fix DataFrame (see `_make_storm_frame`)."""
    return _make_storm_frame


@pytest.fixture
def fake_tropycal(monkeypatch: pytest.MonkeyPatch) -> _FakeState:
    """Inject a fake `tropycal` SDK into `sys.modules`; return its state.

    Seeds one default Katrina-like 2005 Atlantic storm; tests can add more
    via `state.add_storm(...)`.
    """
    state = _FakeState()
    state.add_storm(2005, _make_storm_frame())
    tracks_module = types.ModuleType("tropycal.tracks")
    tracks_module.TrackDataset = _make_trackdataset_cls(state)
    tropycal_module = types.ModuleType("tropycal")
    tropycal_module.tracks = tracks_module
    monkeypatch.setitem(__import__("sys").modules, "tropycal", tropycal_module)
    monkeypatch.setitem(__import__("sys").modules, "tropycal.tracks", tracks_module)
    return state


def _make_recon_obs_frame(
    times: list[str] | None = None,
    lats: list[float] | None = None,
    lons: list[float] | None = None,
) -> pd.DataFrame:
    """Build a fake recon `hdobs.data`-shaped observation frame."""
    times = times or ["2005-08-28 12:00", "2005-08-28 12:10", "2005-08-28 12:20"]
    lats = lats or [25.0, 25.1, 25.2]
    lons = lons or [-85.0, -85.1, -85.2]
    n = len(times)
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "lat": lats,
            "lon": lons,
            "wspd": [120.0, 125.0, 130.0][:n],
            "p_sfc": [945.0, 942.0, 940.0][:n],
            "temp": [22.0, 22.5, 23.0][:n],
        }
    )


class _FakeReconObj:
    """Stand-in for a tropycal recon sub-product exposing `.data`."""

    def __init__(self, data: pd.DataFrame) -> None:
        self.data = data


@pytest.fixture
def make_recon_obs_frame():
    """Factory for a fake recon observation frame (see `_make_recon_obs_frame`)."""
    return _make_recon_obs_frame


@pytest.fixture
def fake_recon(fake_tropycal, monkeypatch: pytest.MonkeyPatch) -> _FakeState:
    """Inject a fake `tropycal.recon` (hdobs/dropsondes/vdms) into sys.modules.

    Builds on `fake_tropycal` (so `tropycal.tracks` is faked too). The default
    obs frame is seeded on the returned state as `.recon_frame`; set it to
    `None` to simulate a storm with no recon data.
    """
    recon_module = types.ModuleType("tropycal.recon")
    fake_tropycal.recon_frame = _make_recon_obs_frame()

    def _builder(storm, data=None, update=False):
        return _FakeReconObj(fake_tropycal.recon_frame)

    recon_module.hdobs = _builder
    recon_module.dropsondes = _builder
    recon_module.vdms = _builder
    monkeypatch.setitem(__import__("sys").modules, "tropycal.recon", recon_module)
    return fake_tropycal


class _FakeRealtime:
    """Stand-in for `tropycal.realtime.Realtime` over the fake state."""

    def __init__(self, state: _FakeState, jtwc: bool = False) -> None:
        self._state = state

    def list_active_storms(self, basin: str = "all") -> list[str]:
        return list(self._state.active_ids)

    def get_storm(self, storm_id: str) -> _FakeStorm:
        return _FakeStorm(self._state.storms[storm_id])


@pytest.fixture
def fake_realtime(fake_tropycal, monkeypatch: pytest.MonkeyPatch) -> _FakeState:
    """Inject a fake `tropycal.realtime` with one active storm.

    Set `state.active_ids = []` to simulate the (common, off-season) case of
    no active storms.
    """
    fake_tropycal.active_ids = ["AL012026"]
    fake_tropycal.storms.setdefault(
        "AL012026", _make_storm_frame(storm_id="AL012026", name="ALPHA")
    )
    module = types.ModuleType("tropycal.realtime")
    module.Realtime = lambda jtwc=False, **kwargs: _FakeRealtime(fake_tropycal, jtwc)
    monkeypatch.setitem(__import__("sys").modules, "tropycal.realtime", module)
    return fake_tropycal


@pytest.fixture
def fake_ships(fake_tropycal) -> _FakeState:
    """Seed a fake SHIPS forecast table on the state (no extra module needed).

    SHIPS is read via `storm.get_ships(time).to_dataframe()`, so faking the
    storm method (already on `_FakeStorm`) is enough — set `.ships_frame` to
    `None` to simulate a cycle with no SHIPS guidance.
    """
    fake_tropycal.ships_frame = pd.DataFrame(
        {
            "fhr": [0, 6, 12],
            "vmax_noland_kt": [80.0, 95.0, 110.0],
            "shear_kt": [10.0, 8.0, 6.0],
            "storm_type": ["HU", "HU", "HU"],
        }
    )
    # Register the storm the ships tests query (the SHIPS table itself comes
    # from `ships_frame`; this just lets get_storm resolve the id).
    fake_tropycal.storms.setdefault("AL092022", _make_storm_frame(storm_id="AL092022"))
    return fake_tropycal


@pytest.fixture
def window() -> tuple[dt.datetime, dt.datetime]:
    """A wide August-September 2005 window covering the default storm."""
    return dt.datetime(2005, 8, 1), dt.datetime(2005, 9, 1)


@pytest.fixture
def gulf_bbox() -> tuple[float, float, float, float]:
    """A (south, north, west, east) bbox over the Gulf of Mexico."""
    return 18.0, 31.0, -98.0, -80.0


@pytest.fixture
def warnings_log() -> Iterator[list[str]]:
    """Capture WARNING-level loguru messages into a list for the test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
