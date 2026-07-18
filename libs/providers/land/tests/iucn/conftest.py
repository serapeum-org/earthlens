"""Shared fixtures for the IUCN backend tests — a faked `requests` session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests


class _FakeResponse:
    """Stand-in for a `requests.Response` with a canned JSON body."""

    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None):
        """Hold the JSON payload, HTTP status code, and headers."""
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        """Return the canned JSON body."""
        return self._payload

    def raise_for_status(self) -> None:
        """Mimic `requests.Response.raise_for_status` for non-2xx statuses.

        The IUCN `_get` shim intercepts every status code path explicitly, so
        in practice this method is never reached on the happy path. It is here
        only so a fake response behaves like the real one if a future shim
        change falls through to `raise_for_status` (the test then surfaces).
        """
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class _FakeSession:
    """Records GET calls and returns a response keyed by the request path.

    For retry tests, a route may carry a *list* of responses; each call pops
    the head, so the same URL fragment can return e.g. `502, 502, 200` across
    three calls.
    """

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
                if isinstance(response, list):
                    item = response[0] if len(response) == 1 else response.pop(0)
                else:
                    item = response
                if isinstance(item, BaseException):
                    raise item
                return item
        return _FakeResponse({})


class _FakeState:
    """Holds the URL-fragment -> response routes and the recorded calls."""

    def __init__(self):
        """Start with no routes and no recorded calls."""
        self.routes: dict[str, _FakeResponse | list] = {}
        self.calls: list[dict] = []

    def route(self, fragment: str, payload: dict, status_code: int = 200) -> None:
        """Map a URL substring to a canned JSON response."""
        self.routes[fragment] = _FakeResponse(payload, status_code)

    def route_queue(self, fragment: str, responses: list) -> None:
        """Map a URL substring to a queue of responses, popped per call."""
        self.routes[fragment] = list(responses)


@pytest.fixture
def fake_iucn(monkeypatch):
    """Install a fake `requests` session and a simulated clock.

    `time.sleep` records its argument and advances simulated `time.monotonic`;
    that way the throttle sees the real elapsed time between calls under test
    (otherwise the throttle would compare against a frozen real clock and
    wrongly sleep `THROTTLE_SECONDS` between every retry).
    """
    state = _FakeState()
    sleeps: list[float] = []
    sim_time = [0.0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        sim_time[0] += seconds

    def fake_monotonic() -> float:
        return sim_time[0]

    monkeypatch.setattr(
        "earthlens.iucn._rest.requests.Session", lambda: _FakeSession(state)
    )
    monkeypatch.setattr("earthlens.iucn._rest.time.sleep", fake_sleep)
    monkeypatch.setattr("earthlens.iucn._rest.time.monotonic", fake_monotonic)
    # Each test starts with a fresh throttle clock so the first call never sleeps.
    from earthlens.iucn._rest import clear_throttle_state

    clear_throttle_state()
    return SimpleNamespace(
        state=state,
        response=_FakeResponse,
        sleeps=sleeps,
        ConnectionError=requests.ConnectionError,
    )
