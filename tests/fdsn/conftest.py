"""Shared fixtures for the FDSN backend tests.

Builds real `obspy.core.event` objects (no network) and a fake
`obspy.clients.fdsn.Client` so the backend can be exercised end-to-end
offline. The `fake_fdsn` fixture patches the client at its source so
the backend's lazy `from obspy.clients.fdsn import Client` picks up the
stub.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from obspy import UTCDateTime
from obspy.core.event import Catalog, Event, Magnitude, Origin


def _make_event(
    *,
    lon: float = 139.0,
    lat: float = 35.0,
    depth_m: float | None = 10000.0,
    mag: float | None = 5.5,
    mag_type: str | None = "Mw",
    time: str = "2024-01-10T00:00:00",
    event_type: str | None = "earthquake",
    status: str | None = "reviewed",
    set_preferred: bool = True,
    extra_first_origin: bool = False,
    with_magnitude: bool = True,
) -> Event:
    """Build one obspy `Event` with controllable fields.

    `extra_first_origin` inserts a decoy origin ahead of the real one
    so a test can prove the preferred-vs-first fallback picks the
    preferred origin. `with_magnitude=False` builds an event with no
    magnitudes at all.
    """
    origin = Origin(
        time=UTCDateTime(time),
        longitude=lon,
        latitude=lat,
        depth=depth_m,
        evaluation_status=status,
    )
    origins = [origin]
    if extra_first_origin:
        decoy = Origin(
            time=UTCDateTime(time),
            longitude=lon + 5.0,
            latitude=lat + 5.0,
            depth=(depth_m + 50000.0) if depth_m is not None else None,
            evaluation_status="preliminary",
        )
        origins = [decoy, origin]
    magnitudes = [Magnitude(mag=mag, magnitude_type=mag_type)] if with_magnitude else []
    event = Event(origins=origins, magnitudes=magnitudes, event_type=event_type)
    if set_preferred:
        event.preferred_origin_id = origin.resource_id
        if magnitudes:
            event.preferred_magnitude_id = magnitudes[0].resource_id
    return event


def _make_catalog(events: list[Event] | None = None) -> Catalog:
    """Build a `Catalog`; defaults to a single nominal event."""
    return Catalog(events=events if events is not None else [_make_event()])


class _FakeClient:
    """Stand-in for `obspy.clients.fdsn.Client` returning a canned result."""

    def __init__(self, state: _FakeFdsn, base_url: str, **kwargs: Any):
        self._state = state
        self.base_url = base_url
        self.kwargs = kwargs

    def get_events(self, **kwargs: Any) -> Catalog:
        self._state.calls.append((self.base_url, dict(kwargs)))
        result = self._state.result_for.get(self.base_url, self._state.default_result)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeFdsn:
    """Callable client factory that records construction + query calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.constructions: list[tuple[str, dict[str, Any]]] = []
        self.default_result: Catalog | BaseException = _make_catalog()
        self.result_for: dict[str, Catalog | BaseException] = {}

    def __call__(self, base_url: str, **kwargs: Any) -> _FakeClient:
        self.constructions.append((base_url, dict(kwargs)))
        return _FakeClient(self, base_url, **kwargs)

    def set_result(self, fdsn_id: str, result: Catalog | BaseException) -> None:
        """Pin the `get_events` result (catalog or exception) for one network."""
        self.result_for[fdsn_id] = result


@pytest.fixture
def make_event() -> Callable[..., Event]:
    """Factory for one obspy `Event` (see `_make_event` kwargs)."""
    return _make_event


@pytest.fixture
def make_catalog() -> Callable[..., Catalog]:
    """Factory for an obspy `Catalog`."""
    return _make_catalog


@pytest.fixture
def fake_fdsn(monkeypatch: pytest.MonkeyPatch) -> _FakeFdsn:
    """Patch `obspy.clients.fdsn.Client` with the recording fake factory."""
    state = _FakeFdsn()
    monkeypatch.setattr("obspy.clients.fdsn.Client", state)
    return state
