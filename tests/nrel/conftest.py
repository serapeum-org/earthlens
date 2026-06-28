"""Shared offline fixtures for the NREL backend tests.

Drives the backend without network by replacing `requests.Session` (looked up
as `earthlens.nrel.backend.requests.Session`) with a recording `FakeSession`
that replays pre-loaded `FakeResponse` payloads built from the trimmed NSRDB /
WTK CSV fixtures under `tests/nrel/fixtures/`. The throttle is neutralised so
the suite never sleeps, and a `nrel_env` fixture supplies dummy credentials so
construction succeeds without touching the network or a real key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import earthlens.nrel._helpers as helpers
import earthlens.nrel.backend as backend

FIXTURES = Path(__file__).parent / "fixtures"


def load_csv(name: str) -> str:
    """Load a trimmed NSRDB / WTK CSV fixture by file stem."""
    return (FIXTURES / f"{name}.csv").read_text(encoding="utf-8")


class FakeResponse:
    """Minimal stand-in for a `requests.Response` over a fixed body."""

    def __init__(
        self, text: str = "", status_code: int = 200, payload: Any = None
    ) -> None:
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        """Return the JSON payload, raising `ValueError` when there is none."""
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        """Raise an `HTTPError` mirroring a real error status."""
        import requests

        raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Recording `requests.Session` that replays configured responses."""

    def __init__(self, responses: Any) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def __enter__(self) -> FakeSession:
        """Enter the context manager (mirrors `requests.Session`)."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit the context manager (no-op; nothing to release)."""
        return None

    def get(self, url: str, timeout: int = 120) -> FakeResponse:
        """Record the URL and return the next (or only) configured response."""
        self.calls.append(url)
        if isinstance(self._responses, list):
            index = min(len(self.calls) - 1, len(self._responses) - 1)
            return self._responses[index]
        return self._responses


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the 1 req/s throttle so the suite never sleeps."""
    monkeypatch.setattr(helpers, "MIN_INTERVAL", 0.0)


@pytest.fixture
def nrel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy NREL credentials in the environment for construction."""
    monkeypatch.setenv("NREL_API_KEY", "DUMMYKEY")
    monkeypatch.setenv("NREL_EMAIL", "tester@example.com")


@pytest.fixture
def nsrdb_csv() -> str:
    """The trimmed NSRDB hourly CSV fixture (2 metadata rows, 3 data rows)."""
    return load_csv("nsrdb_sample")


@pytest.fixture
def wtk_csv() -> str:
    """The trimmed WIND Toolkit CSV fixture (1 metadata row, 3 data rows)."""
    return load_csv("wtk_sample")


@pytest.fixture
def loguru_messages():
    """Capture loguru WARNING+ messages into a list for the duration of a test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def bind_session(monkeypatch: pytest.MonkeyPatch):
    """Return a binder that patches `requests.Session` to a `FakeSession`."""

    def _bind(responses: Any) -> FakeSession:
        session = FakeSession(responses)
        monkeypatch.setattr(backend.requests, "Session", lambda: session)
        return session

    return _bind
