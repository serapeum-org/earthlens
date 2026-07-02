"""Shared offline fixtures for the AirNow backend tests.

Drives the backend end-to-end without network: a recording fake
`requests.Session` is injected via `session=` so the lazily-built
`AirnowClient` picks it up, and the client's `time.sleep` is neutered so
`429` back-off costs no wall-clock time. Builds plain dict rows shaped
like an AirNow `/aq/data/` verbose JSON observation.
"""

from __future__ import annotations

from typing import Any

import pytest


def _observation(
    *,
    parameter: str = "PM2.5",
    concentration: float = 12.3,
    unit: str = "UG/M3",
    aqi: float = 51,
    category: float = 2,
    lat: float = 34.1,
    lon: float = -118.2,
    utc: str = "2026-01-01T00:00",
    site: str = "Los Angeles",
    agency: str = "South Coast AQMD",
    aqs: str = "060370113",
) -> dict[str, Any]:
    """Build one AirNow verbose observation row."""
    return {
        "Latitude": lat,
        "Longitude": lon,
        "UTC": utc,
        "Parameter": parameter,
        "Value": concentration,
        "RawConcentration": concentration,
        "Unit": unit,
        "AQI": aqi,
        "Category": category,
        "SiteName": site,
        "AgencyName": agency,
        "FullAQSCode": aqs,
    }


class _FakeResponse:
    """Canned `requests`-like response with json/raise_for_status."""

    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeAirnow:
    """Recording transport: returns `rows` after emitting `n_429` retries."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = rows if rows is not None else [_observation()]
        self.n_429: int = 0

    def respond(self, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append(params)
        if self.n_429 > 0:
            self.n_429 -= 1
            return _FakeResponse([], status_code=429, headers={"Retry-After": "0"})
        return _FakeResponse(self.rows)


class _FakeSession:
    """Stand-in for `requests.Session` delegating GETs to the state."""

    def __init__(self, state: _FakeAirnow) -> None:
        self._state = state

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._state.respond(dict(params or {}))


@pytest.fixture
def fake_airnow(monkeypatch: pytest.MonkeyPatch) -> _FakeAirnow:
    """Recording fake AirNow transport with back-off sleep neutered."""
    state = _FakeAirnow()
    monkeypatch.setattr("earthlens.airnow.client.time.sleep", lambda seconds: None)
    return state


@pytest.fixture
def make_observation():
    """Factory for an AirNow observation row (see `_observation`)."""
    return _observation


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
