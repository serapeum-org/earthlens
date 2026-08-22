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


def build_release_workbook(path: Path, rows: list[tuple[str, str, object]]) -> Path:
    """Write a minimal stand-in for the INFORM Risk release workbook.

    Mirrors the published layout the parser relies on: a year-named score sheet,
    a banner row above the header, and one column per dimension.

    Args:
        path: The `.xlsx` path to write.
        rows: `(country, iso3, inform_score)` triples; the dimension columns are
            derived so each row is internally consistent.

    Returns:
        Path: The written workbook.
    """
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "INFORM Risk 2026 (a-z)"
    sheet.append(["INFORM Risk 2026 - results"])
    sheet.append(
        [
            "COUNTRY",
            "ISO3",
            "INFORM RISK",
            "HAZARD & EXPOSURE",
            "VULNERABILITY",
            "LACK OF COPING CAPACITY",
        ]
    )
    # The published sheet puts a units legend directly under the header, so the
    # stand-in carries one too - the parser has to drop it by row shape.
    sheet.append(["(a-z)", "(a-z)", "(0-10)", "(0-10)", "(0-10)", "(0-10)"])
    for country, iso3, score in rows:
        sheet.append([country, iso3, score, score, score, score])
    book.create_sheet("About")
    book.save(path)
    return path


@pytest.fixture(autouse=True)
def _forget_discovered_release() -> Iterator[None]:
    """Clear the process-wide release-URL memo around every test.

    Yields:
        None: Control to the test, with the memo empty on both sides.
    """
    _helpers.clear_release_cache()
    yield
    _helpers.clear_release_cache()


@pytest.fixture
def make_release_workbook():
    """Return the stand-in release-workbook builder, for a test that needs its own.

    Returns:
        Callable: :func:`build_release_workbook`.
    """
    return build_release_workbook


@pytest.fixture(scope="session")
def release_workbook(tmp_path_factory) -> Path:
    """A three-country stand-in workbook, built once for the session."""
    target = tmp_path_factory.mktemp("inform-release") / "INFORM_Risk_2026_v072.xlsx"
    return build_release_workbook(
        target, [("Kenya", "KEN", 6.2), ("Zambia", "ZMB", 5.1), ("Nowhere", "NOW", "x")]
    )


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Collect every INFO-and-above loguru line a test provokes.

    Yields:
        list[str]: The formatted records, appended as they are emitted.
    """
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


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

    @property
    def text(self) -> str:
        """Return the payload as text, for the HTML the results page serves."""
        return (
            self._payload
            if isinstance(self._payload, str)
            else json.dumps(self._payload)
        )


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
