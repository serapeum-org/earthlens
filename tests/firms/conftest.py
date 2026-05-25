"""Shared fixtures for the FIRMS backend tests.

Provides a fake `requests.get` (patched at the backend module) that
returns canned CSV bodies and records the request URLs, plus a loguru
`warnings_log` capture, so the backend runs end-to-end offline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

#: A minimal valid VIIRS area-CSV body (header + one detection row).
VIIRS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
    "34.0,-118.0,320.5,0.4,0.4,2024-08-01,1325,N,VIIRS,n,2.0NRT,295.0,12.5,D\n"
)

#: A header-only (no detections) CSV body.
EMPTY_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
)


class _FakeResponse:
    """Stand-in for a `requests.Response` with a canned text body."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeFirms:
    """Callable `requests.get` stand-in recording URLs and serving bodies.

    By default every call returns :data:`VIIRS_CSV`. Set `responses` to
    a list of `_FakeResponse` to serve a scripted sequence (popped in
    order), e.g. a 429 then a CSV for the back-off test.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.text: str = VIIRS_CSV
        self.status_code: int = 200
        self.responses: list[_FakeResponse] | None = None

    def __call__(self, url: str, timeout: float) -> _FakeResponse:
        self.calls.append(url)
        if self.responses is not None:
            return self.responses.pop(0)
        return _FakeResponse(self.text, self.status_code)


@pytest.fixture
def fake_firms(monkeypatch: pytest.MonkeyPatch) -> _FakeFirms:
    """Patch `requests.get` in the FIRMS backend with the recording fake."""
    state = _FakeFirms()
    monkeypatch.setattr("earthlens.firms.backend.requests.get", state)
    return state


@pytest.fixture
def warnings_log() -> Iterator[list[str]]:
    """Capture WARNING-level loguru messages into a list for the test."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
