"""Shared fixtures for the risk-indicators tests — captured JSON, no network.

Every fixture here is offline: a `fake_http` recorder patches
`earthlens.risk_indicators._helpers.requests.get` to route a request to the
right captured JSON by URL, so no test touches ThinkHazard / INFORM / GFW. The
captured payloads under `data/` are real responses with the GFW key removed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from earthlens.risk_indicators import _helpers

DATA = Path(__file__).parent / "data"


@pytest.fixture
def captured_warnings() -> Iterator[list[str]]:
    """Collect the loguru WARNING lines a test provokes.

    Yields:
        list[str]: The formatted records, appended as they are emitted.
    """
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def load_json(name: str) -> Any:
    """Read a captured JSON fixture by file name.

    Args:
        name: The fixture file name under `data/`.

    Returns:
        The parsed JSON.
    """
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class _FakeResponse:
    """A minimal stand-in for `requests.Response` over a JSON payload."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """No-op: the captured responses are always 200."""

    def json(self) -> Any:
        """Return the captured payload."""
        return self._payload


class FakeHttp:
    """Records `requests.get` calls and routes each to a captured fixture."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        """Route a GET to the captured payload for its URL.

        Args:
            url: The request URL.
            params: Query parameters.
            headers: Request headers (recorded so tests can assert `x-api-key`).
            timeout: Ignored.

        Returns:
            _FakeResponse: The captured payload wrapped as a response.

        Raises:
            AssertionError: If no fixture matches the URL.
        """
        self.calls.append(
            {"url": url, "params": params or {}, "headers": headers or {}}
        )
        if "/geostore/admin/" in url:
            geojson = load_json("gfw_geostore_admin_KEN.json")
            payload: Any = {"data": {"attributes": {"geojson": geojson}}}
        elif "/query/json" in url:
            payload = load_json("gfw_tcl_iso_change_KEN.json")
        elif "/countries/Scores" in url:
            # Captured from workflow 505 (the 2026 release) on 2026-06-27 and kept
            # for its response *shape*, which is release-independent: the payload
            # is a four-row truncation with no workflow field in it. Tests that
            # assert which workflow was requested read `calls[...]["params"]`, so
            # they are unaffected by the release this capture came from.
            payload = load_json("inform_scores_wf505_2026.json")
        elif url.endswith("/FL.json"):
            payload = load_json("thinkhazard_report_133_FL.json")
        elif "/report/" in url:
            payload = load_json("thinkhazard_report_133.json")
        else:  # pragma: no cover - guards an unrouted URL in a new test
            raise AssertionError(f"no fixture routed for URL {url!r}")
        return _FakeResponse(payload)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """Patch `_helpers.requests.get` with a recording router; yield the recorder.

    Args:
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        FakeHttp: The recorder, exposing `.calls` for header/URL assertions.
    """
    recorder = FakeHttp()
    monkeypatch.setattr(_helpers.requests, "get", recorder.get)
    return recorder
