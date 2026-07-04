"""Shared offline fixtures for the glaciers tests — captured data, no network.

Every fixture is offline. `data/rgi_sample.zip` is a 6-glacier subset of RGI
region 11 (Central Europe), `data/wgms_sample.zip` carries trimmed FoG tables
(`mass_balance` / `front_variation` / `state` / `glacier`), and
`data/glims_sample.geojson` is a 5-feature GLIMS WFS response. The fake HTTP
helpers route a download / WFS call to one of these so no test touches IHP-WINS,
GLIMS, or WGMS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

DATA = Path(__file__).parent / "data"


class _FakeStreamResponse:
    """A streaming `requests.Response` stand-in backed by local bytes."""

    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeStreamResponse:
        """Enter the `with get(...)` block."""
        return self

    def __exit__(self, *exc: Any) -> None:
        """Exit the `with` block (no resources to release)."""

    def raise_for_status(self) -> None:
        """No-op: the local fixtures are always 200."""

    def iter_content(self, chunk_size: int = 1 << 20):
        """Yield the payload in `chunk_size` chunks."""
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]

    def close(self) -> None:
        """No-op: the fake holds no socket."""


class _FakeTextResponse:
    """A non-streaming `requests.Response` stand-in over a text payload."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """No-op: the captured WFS response is always 200."""


class FakeHttp:
    """Records `requests.get` calls and serves a local fixture per URL."""

    def __init__(self, zip_payload: bytes | None, wfs_text: str | None) -> None:
        self._zip_payload = zip_payload
        self._wfs_text = wfs_text
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        params: dict | None = None,
        stream: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Route a GET to the streaming zip or the WFS text fixture.

        Args:
            url: The request URL.
            params: Query parameters (recorded so tests can assert the bbox).
            stream: `True` for a download (zip), `False` for the WFS query.
            timeout: Ignored.
            **kwargs: Extra transport kwargs (e.g. `HttpClient`'s `headers=`).

        Returns:
            A fake streaming or text response.

        Raises:
            AssertionError: If the requested fixture was not provided.
        """
        self.calls.append({"url": url, "params": params or {}, "stream": stream})
        if stream:
            assert self._zip_payload is not None, "no zip payload configured"
            return _FakeStreamResponse(self._zip_payload)
        assert self._wfs_text is not None, "no WFS text configured"
        return _FakeTextResponse(self._wfs_text)


@pytest.fixture
def rgi_sample_zip() -> Path:
    """Path to the 6-glacier RGI region-11 sample zip."""
    return DATA / "rgi_sample.zip"


@pytest.fixture
def wgms_sample_zip() -> Path:
    """Path to the trimmed WGMS FoG sample zip."""
    return DATA / "wgms_sample.zip"


@pytest.fixture
def glims_sample_geojson() -> Path:
    """Path to the 5-feature GLIMS WFS GeoJSON sample."""
    return DATA / "glims_sample.geojson"


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """Patch `_helpers.requests.get` to serve the local zip + WFS fixtures.

    Args:
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        FakeHttp: The recorder, exposing `.calls` for URL / bbox assertions.
    """
    from earthlens.glaciers import _helpers

    recorder = FakeHttp(
        zip_payload=(DATA / "wgms_sample.zip").read_bytes(),
        wfs_text=(DATA / "glims_sample.geojson").read_text(encoding="utf-8"),
    )
    monkeypatch.setattr(_helpers.requests, "get", recorder.get)
    return recorder
