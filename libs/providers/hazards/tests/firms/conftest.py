"""Shared fixtures for the FIRMS backend tests.

Provides a fake `requests.get` (patched at the backend module) that
returns canned CSV bodies and records the request URLs, plus a loguru
`warnings_log` capture, so the backend runs end-to-end offline.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from earthlens.testing import FakeResponse

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


class _FakeFirms:
    """Callable `requests.get` stand-in recording URLs and serving bodies.

    By default every call returns :data:`VIIRS_CSV`. Set `responses` to
    a list of `FakeResponse` to serve a scripted sequence (popped in
    order), e.g. a 429 then a CSV for the back-off test.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.text: str = VIIRS_CSV
        self.status_code: int = 200
        self.responses: list[FakeResponse] | None = None

    def __call__(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(url)
        if self.responses is not None:
            resp = self.responses.pop(0)
            resp.url = url  # mirror the requested URL (carries the MAP_KEY)
            return resp
        return FakeResponse(text=self.text, status_code=self.status_code, url=url)


@pytest.fixture
def fake_firms(monkeypatch: pytest.MonkeyPatch) -> _FakeFirms:
    """Patch `requests.get` in the FIRMS backend with the recording fake.

    Also sets a dummy `FIRMS_MAP_KEY` so `download()` resolves a key from
    the environment via its lazy `authenticate()`; tests that need a
    specific key call `backend.authenticate(api_key=...)` to override.
    """
    monkeypatch.setenv("FIRMS_MAP_KEY", "k")
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
