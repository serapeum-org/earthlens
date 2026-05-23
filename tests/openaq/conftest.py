"""Shared offline fixtures for the OpenAQ backend tests.

Drives the backend end-to-end without network: a recording fake
`requests.Session` is patched in at the source so the lazily-built
`OpenaqClient` picks up the stub, and the client's `time.sleep` is
neutered so `429` back-off costs no wall-clock time. Builds plain
dict payloads shaped like the OpenAQ v3 `{"meta", "results"}`
envelope.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest


def _location(
    *,
    station_id: int = 1,
    lat: float = 34.1,
    lon: float = -118.2,
    provider: str | None = "AirNow",
    sensors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one OpenAQ location object with a single pm25 sensor by default."""
    if sensors is None:
        sensors = [_sensor()]
    body: dict[str, Any] = {
        "id": station_id,
        "coordinates": {"latitude": lat, "longitude": lon},
        "sensors": sensors,
    }
    if provider is not None:
        body["provider"] = {"name": provider}
    return body


def _sensor(
    *,
    sensor_id: int = 10,
    param_id: int = 2,
    name: str = "pm25",
    units: str = "µg/m³",
) -> dict[str, Any]:
    """Build one sensor object carrying its parameter id/name/units."""
    return {
        "id": sensor_id,
        "parameter": {"id": param_id, "name": name, "units": units},
    }


def _measurement(
    *, value: float = 12.3, utc: str = "2024-01-01T00:00:00Z"
) -> dict[str, Any]:
    """Build one v3 measurement object (period.datetimeFrom.utc shape)."""
    return {"value": value, "period": {"datetimeFrom": {"utc": utc}}}


class _FakeResponse:
    """Canned `requests`-like response with json/raise_for_status."""

    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeOpenaq:
    """Recording transport state shared by the patched session.

    Returns `locations` for the locations endpoint and `measurements`
    for any sensor endpoint, after emitting `n_429` rate-limit
    responses (with `Retry-After: 0`) to exercise back-off. Every GET
    is recorded on `calls` as `(url, params)`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.locations: dict[str, Any] = {"results": [_location()]}
        self.measurements: dict[str, Any] = {"results": [_measurement()]}
        self.n_429: int = 0

    def respond(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, params))
        # The real v3 list endpoints paginate: page 1 returns the rows,
        # later pages return an empty `results` to signal the end. Model
        # that so the client's pagination loop terminates.
        page = int(params.get("page", 1))
        if "/locations" in url:
            body = self.locations if page == 1 else {"results": []}
            return _FakeResponse(body)
        if self.n_429 > 0:
            self.n_429 -= 1
            return _FakeResponse({}, status_code=429, headers={"Retry-After": "0"})
        body = self.measurements if page == 1 else {"results": []}
        return _FakeResponse(body)

    def location_calls(self) -> list[dict[str, Any]]:
        """Return the params of every locations request made."""
        return [params for url, params in self.calls if "/locations" in url]

    def measurement_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Return the (url, params) of every sensor-measurement request."""
        return [(url, params) for url, params in self.calls if "/sensors/" in url]


class _FakeSession:
    """Stand-in for `requests.Session` delegating GETs to the state."""

    def __init__(self, state: _FakeOpenaq) -> None:
        self._state = state

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._state.respond(url, dict(params or {}))


@pytest.fixture
def fake_openaq(monkeypatch: pytest.MonkeyPatch) -> _FakeOpenaq:
    """Patch the client's session factory + sleep with the recording fake."""
    state = _FakeOpenaq()
    monkeypatch.setattr(
        "earthlens.openaq.client.requests.Session", lambda: _FakeSession(state)
    )
    monkeypatch.setattr("earthlens.openaq.client.time.sleep", lambda seconds: None)
    return state


@pytest.fixture
def log_messages():
    """Collect loguru log messages into a list (loguru bypasses caplog)."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="WARNING"
    )
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def make_location() -> Callable[..., dict[str, Any]]:
    """Factory for an OpenAQ location object (see `_location` kwargs)."""
    return _location


@pytest.fixture
def make_sensor() -> Callable[..., dict[str, Any]]:
    """Factory for an OpenAQ sensor object."""
    return _sensor


@pytest.fixture
def make_measurement() -> Callable[..., dict[str, Any]]:
    """Factory for an OpenAQ measurement object."""
    return _measurement
