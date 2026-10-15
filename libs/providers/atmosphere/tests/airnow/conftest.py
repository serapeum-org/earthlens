"""Shared offline fixtures for the AirNow backend tests.

Drives the backend end-to-end without network: a recording fake
`requests.Session` is injected via `session=` so the lazily-built
`AirnowClient` picks it up. Builds plain dict rows shaped like an AirNow
`/aq/data/` verbose JSON observation. The `429` back-off tests inject a
capturing `sleep=` directly into `AirnowClient` (see `test_client.py`),
so no monkeypatch of `time.sleep` is needed here.
"""

from __future__ import annotations

from typing import Any

from earthlens.testing import FakeResponse


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


class _FakeAirnow:
    """Recording transport: returns `rows` after emitting `n_429` retries."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = rows if rows is not None else [_observation()]
        self.n_429: int = 0

    def respond(self, params: dict[str, Any]) -> FakeResponse:
        self.calls.append(params)
        if self.n_429 > 0:
            self.n_429 -= 1
            return FakeResponse(json=[], status_code=429, headers={"Retry-After": "0"})
        return FakeResponse(json=self.rows)


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
    ) -> FakeResponse:
        return self._state.respond(dict(params or {}))
