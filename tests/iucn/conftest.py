"""Shared fixtures for the IUCN backend tests — a faked `requests` session."""

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
    """Records GET calls and returns a response keyed by the request path."""

    def __init__(self, state: _FakeState):
        """Bind the shared state holding routed responses and calls."""
        self._state = state

    def get(self, url, params=None, headers=None, timeout=None):
        """Record the call and return the response routed by URL substring."""
        self._state.calls.append(
            {"url": url, "params": params or {}, "headers": headers or {}}
        )
        for fragment, response in self._state.routes.items():
            if fragment in url:
                return response
        return _FakeResponse({})


class _FakeState:
    """Holds the URL-fragment -> response routes and the recorded calls."""

    def __init__(self):
        """Start with no routes and no recorded calls."""
        self.routes: dict[str, _FakeResponse] = {}
        self.calls: list[dict] = []

    def route(self, fragment: str, payload: dict, status_code: int = 200) -> None:
        """Map a URL substring to a canned JSON response."""
        self.routes[fragment] = _FakeResponse(payload, status_code)


@pytest.fixture
def fake_iucn(monkeypatch):
    """Install a fake `requests` session and neuter the throttle sleep."""
    state = _FakeState()
    monkeypatch.setattr(
        "earthlens.iucn._rest.requests.Session", lambda: _FakeSession(state)
    )
    monkeypatch.setattr("earthlens.iucn._rest.time.sleep", lambda seconds: None)
    return SimpleNamespace(state=state, response=_FakeResponse)
