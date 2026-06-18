"""Shared fixtures for the WDPA backend tests — a faked `requests` session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeResponse:
    """Stand-in for a `requests.Response` with a canned JSON body."""

    def __init__(self, payload: dict, status_code: int = 200):
        """Hold the JSON payload and HTTP status code."""
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        """Return the canned JSON body."""
        return self._payload

    def raise_for_status(self) -> None:
        """No-op for the 2xx responses these tests use."""


class _FakeSession:
    """Records GET calls and returns queued responses in order."""

    def __init__(self, state: _FakeState):
        """Bind the shared state holding queued responses and calls."""
        self._state = state

    def get(self, url, params=None, timeout=None):
        """Record the call and return the next queued response."""
        self._state.calls.append({"url": url, "params": params or {}})
        index = min(len(self._state.calls) - 1, len(self._state.responses) - 1)
        return self._state.responses[index]


class _FakeState:
    """Holds the queued responses and the recorded GET calls."""

    def __init__(self):
        """Start with one empty search page and no recorded calls."""
        self.responses: list[_FakeResponse] = [
            _FakeResponse({"protected_areas": []})
        ]
        self.calls: list[dict] = []

    def set_responses(self, responses: list[_FakeResponse]) -> None:
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
    """Install a fake `requests.Session` for the WDPA REST client."""
    state = _FakeState()
    monkeypatch.setattr(
        "earthlens.wdpa._rest.requests.Session", lambda: _FakeSession(state)
    )
    return SimpleNamespace(
        state=state,
        response=_FakeResponse,
        area=_area,
        polygon=_polygon,
    )
