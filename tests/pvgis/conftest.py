"""Shared offline fixtures for the PVGIS backend tests.

Drives the backend without network by replacing `requests.Session` (looked up
as `earthlens.pvgis.backend.requests.Session`) with a recording `FakeSession`
that returns pre-loaded `FakeResponse` payloads built from the captured
`seriescalc` / `tmy` fixture JSON under `tests/pvgis/fixtures/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import earthlens.pvgis.backend as backend

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a captured PVGIS JSON fixture by file stem."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    """Minimal stand-in for a `requests.Response` over a fixed payload."""

    def __init__(self, payload: Any, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

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

    def get(self, url: str, timeout: int = 60) -> FakeResponse:
        """Record the URL and return the next (or only) configured response."""
        self.calls.append(url)
        if isinstance(self._responses, list):
            index = min(len(self.calls) - 1, len(self._responses) - 1)
            return self._responses[index]
        return self._responses


@pytest.fixture
def seriescalc_payload() -> dict[str, Any]:
    """The captured `seriescalc` JSON response (24 hourly records)."""
    return load_fixture("seriescalc_sample")


@pytest.fixture
def tmy_payload() -> dict[str, Any]:
    """The captured `tmy` JSON response (24 hourly records)."""
    return load_fixture("tmy_sample")


@pytest.fixture
def bind_session(monkeypatch: pytest.MonkeyPatch):
    """Return a binder that patches `requests.Session` to a `FakeSession`."""

    def _bind(responses: Any) -> FakeSession:
        session = FakeSession(responses)
        monkeypatch.setattr(backend.requests, "Session", lambda: session)
        return session

    return _bind
