"""Shared fixtures for the GDACS backend tests.

Builds hand-made GDACS GeoJSON payloads (no network) and a fake
`requests.get` so the backend can be exercised end-to-end offline. The
`fake_gdacs` fixture patches `requests.get` at the backend module so
the SEARCH call returns a canned response and records its query params.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

import pytest
import requests


def _make_feature(
    *,
    lon: float = 12.5,
    lat: float = 42.0,
    eventtype: str = "EQ",
    eventid: int | None = 1541788,
    episodeid: int | None = 1707034,
    name: str = "Earthquake in Italy",
    alertlevel: str = "Green",
    alertscore: float = 1.0,
    fromdate: str = "2026-05-10T00:00:00",
    todate: str = "2026-05-10T01:00:00",
    country: str = "Italy",
    iso3: str = "ITA",
    glide: str = "",
    severity: float | None = 4.7,
    severityunit: str | None = "M",
    severitytext: str | None = "Magnitude 4.7M",
    geometry: dict[str, Any] | None = None,
    drop_properties: tuple[str, ...] = (),
    drop_severity: bool = False,
) -> dict[str, Any]:
    """Build one GDACS GeoJSON feature with controllable fields.

    `drop_properties` removes named keys to exercise the defensive
    `.get`; `drop_severity` omits the whole `severitydata` sub-dict;
    `geometry` overrides the default Point (pass an explicit dict or
    `None` to drop geometry entirely via `drop_properties`).
    """
    properties: dict[str, Any] = {
        "eventtype": eventtype,
        "eventid": eventid,
        "episodeid": episodeid,
        "name": name,
        "alertlevel": alertlevel,
        "alertscore": alertscore,
        "fromdate": fromdate,
        "todate": todate,
        "country": country,
        "iso3": iso3,
        "glide": glide,
    }
    if not drop_severity:
        properties["severitydata"] = {
            "severity": severity,
            "severitytext": severitytext,
            "severityunit": severityunit,
        }
    for key in drop_properties:
        properties.pop(key, None)
    feature_geometry = (
        geometry
        if geometry is not None
        else {"type": "Point", "coordinates": [lon, lat]}
    )
    return {"type": "Feature", "geometry": feature_geometry, "properties": properties}


def _make_payload(features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Wrap features in a GDACS GeoJSON FeatureCollection envelope."""
    return {
        "type": "FeatureCollection",
        "features": features if features is not None else [_make_feature()],
    }


class _FakeResponse:
    """Stand-in for a `requests.Response` returning a canned payload."""

    def __init__(
        self,
        payload: dict[str, Any],
        status_error: Exception | None,
        status_code: int = 200,
    ):
        self._payload = payload
        self._status_error = status_error
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error
        if self.status_code >= 400:
            # Neutral label (the response carries the real status_code, which is
            # what gdacs_http_status reads first) so it is never mis-labelled.
            raise requests.HTTPError(f"{self.status_code} HTTP Error", response=self)

    def json(self) -> dict[str, Any]:
        return self._payload

    def close(self) -> None:
        """Match the `requests.Response.close` shape (no-op)."""
        return None


class _FakeGdacs:
    """Callable `requests.get` stand-in that records the query params."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload: dict[str, Any] = _make_payload()
        self.status_error: Exception | None = None
        self.status_code: int = 200

    def __call__(self, url: str, **kwargs: Any) -> _FakeResponse:
        entry: dict[str, Any] = {"url": url}
        entry.update(kwargs)
        self.calls.append(entry)
        return _FakeResponse(self.payload, self.status_error, self.status_code)

    def set_payload(self, payload: dict[str, Any]) -> None:
        """Pin the payload the next SEARCH call returns."""
        self.payload = payload

    def set_status_error(self, error: Exception) -> None:
        """Make `raise_for_status` raise the given error (HTTP failure)."""
        self.status_error = error

    def set_retry_status(self, code: int) -> None:
        """Return this HTTP status on every call, so the client retry loop engages.

        Unlike `set_status_error` (which raises only from `raise_for_status`, so
        the response looks like a `200` to the client and is never retried), this
        sets the real `status_code`, so a code in the client's `status_forcelist`
        drives the actual retry path.
        """
        self.status_code = code


@pytest.fixture
def make_feature() -> Callable[..., dict[str, Any]]:
    """Factory for one GDACS GeoJSON feature (see `_make_feature` kwargs)."""
    return _make_feature


@pytest.fixture
def make_payload() -> Callable[..., dict[str, Any]]:
    """Factory for a GDACS GeoJSON FeatureCollection payload."""
    return _make_payload


@pytest.fixture
def fake_gdacs(monkeypatch: pytest.MonkeyPatch) -> _FakeGdacs:
    """Patch `requests.get` in the backend with the recording fake."""
    state = _FakeGdacs()
    monkeypatch.setattr("earthlens.gdacs.backend.requests.get", state)
    return state


@pytest.fixture
def warnings_log() -> Iterator[list[str]]:
    """Capture WARNING-level loguru messages into a list for the test's duration."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
