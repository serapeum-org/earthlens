"""Shared fixtures for the WDPA backend tests — a faked `requests` session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from earthlens.testing import FakeResponse


class _FakeSession:
    """Records GET calls and returns queued responses or raises transport errors."""

    def __init__(self, state: _FakeState):
        """Bind the shared state holding queued responses and calls."""
        self._state = state

    def get(self, url, params=None, timeout=None, **kwargs):
        """Record the call and either raise the queued exception or return the next response."""
        self._state.calls.append({"url": url, "params": params or {}})
        index = min(len(self._state.calls) - 1, len(self._state.responses) - 1)
        item = self._state.responses[index]
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeState:
    """Holds the queued responses and the recorded GET calls."""

    def __init__(self):
        """Start with one empty search page and no recorded calls."""
        self.responses: list[FakeResponse] = [
            FakeResponse(json={"protected_areas": []})
        ]
        self.calls: list[dict] = []

    def set_responses(self, responses: list[FakeResponse]) -> None:
        """Pin the sequence of responses `get` returns, one per call."""
        self.responses = responses


def _polygon(coords=None):
    """Build a GeoJSON Polygon geometry dict."""
    ring = coords or [[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]
    return {"type": "Polygon", "coordinates": [ring]}


def _area(wdpa_id="555", geometry=None, **fields):
    """Build a protected-area JSON record with an embedded polygon geometry."""
    area = {
        "wdpa_id": wdpa_id,
        "name": "Test Park",
        "marine": False,
        "designation": {"name": "National Park"},
        "iucn_category": {"name": "II"},
        "countries": [{"iso_3": "KEN"}],
        "geojson": {"geometry": geometry if geometry is not None else _polygon()},
    }
    area.update(fields)
    return area


@pytest.fixture
def fake_wdpa(monkeypatch):
    """Install a fake `requests.Session` for the WDPA REST client; neuter sleep."""
    state = _FakeState()
    sleeps: list[float] = []
    monkeypatch.setattr(
        "earthlens.wdpa._rest.requests.Session", lambda: _FakeSession(state)
    )
    monkeypatch.setattr(
        "earthlens.wdpa._rest.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    return SimpleNamespace(
        state=state,
        response=FakeResponse,
        area=_area,
        polygon=_polygon,
        sleeps=sleeps,
        ConnectionError=requests.ConnectionError,
    )
