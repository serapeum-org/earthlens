"""Unit tests for `earthlens.base.http` (headers, retry, download)."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from earthlens.base.http import (
    DEFAULT_STATUS_FORCELIST,
    HttpClient,
    _default_user_agent,
    _parse_retry_after,
)


class _Resp:
    """Canned response: json/raise_for_status/iter_content/close."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: Any = None,
        blocks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._blocks = blocks or []
        self.closed = False

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int | None = None) -> Any:
        yield from self._blocks

    def close(self) -> None:
        self.closed = True


class _RecordingSession:
    """Returns queued responses in order, recording each verb call."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)


class _RequestOnlySession:
    """Implements only `request()` — exercises the `_send` fallback."""

    def __init__(self, response: _Resp) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append((method, url, kwargs))
        return self._response


def _client(
    responses: list[_Resp], **kwargs: Any
) -> tuple[HttpClient, _RecordingSession, list[float]]:
    """Build a client over a recording session with captured sleeps."""
    session = _RecordingSession(responses)
    waits: list[float] = []
    client = HttpClient(session=session, sleep=waits.append, **kwargs)
    return client, session, waits


@pytest.mark.unit
class TestParseRetryAfter:
    """Retry-After header parsing."""

    @pytest.mark.parametrize(
        "value, expected",
        [("5", 5.0), ("0", 0.0), (None, None), ("soon", None), ("", None)],
    )
    def test_parse(self, value: str | None, expected: float | None):
        """Numeric values parse to seconds; missing/junk yield None."""
        assert _parse_retry_after(value) == expected


@pytest.mark.unit
class TestDefaults:
    """Construction defaults: user-agent, headers, forcelist."""

    def test_default_user_agent_is_version_stamped(self):
        """The default agent is the earthlens/{version} string."""
        assert _default_user_agent().startswith("earthlens/")

    def test_default_user_agent_is_not_mozilla(self):
        """The default agent is non-Mozilla (DIGITAL.CSIC Anubis pass)."""
        assert "mozilla" not in _default_user_agent().lower()

    def test_default_headers_carry_ua_and_gzip(self):
        """The default headers set User-Agent and gzip Accept-Encoding."""
        headers = HttpClient().default_headers
        assert headers["User-Agent"].startswith("earthlens/")
        assert headers["Accept-Encoding"] == "gzip, deflate"

    def test_custom_user_agent_overrides_default(self):
        """A user_agent= argument replaces the default agent string."""
        assert HttpClient(user_agent="osm-contact/1.0").default_headers[
            "User-Agent"
        ] == "osm-contact/1.0"

    def test_extra_headers_merge_into_defaults(self):
        """Construction headers= merge onto the defaults (e.g. X-API-Key)."""
        headers = HttpClient(headers={"X-API-Key": "k"}).default_headers
        assert headers["X-API-Key"] == "k"
        assert "User-Agent" in headers

    def test_default_forcelist_includes_429_and_5xx(self):
        """The default retry set covers 429 and the transient 5xx family."""
        assert DEFAULT_STATUS_FORCELIST == (429, 500, 502, 503, 504)
        assert HttpClient().status_forcelist == (429, 500, 502, 503, 504)

    def test_session_property_exposes_injected_session(self):
        """The session property returns the injected transport."""
        session = _RecordingSession([])
        assert HttpClient(session=session).session is session


@pytest.mark.unit
class TestRequest:
    """Verb dispatch, header merge, and JSON decode."""

    def test_get_sends_merged_headers(self):
        """Per-request headers merge over (and override) the defaults."""
        client, session, _ = _client([_Resp(body={})], headers={"X-API-Key": "k"})
        client.get("http://x", headers={"X-Extra": "v", "User-Agent": "override"})
        sent = session.calls[0][2]["headers"]
        assert sent["X-API-Key"] == "k"
        assert sent["X-Extra"] == "v"
        assert sent["User-Agent"] == "override"
        assert sent["Accept-Encoding"] == "gzip, deflate"

    def test_get_applies_default_timeout(self):
        """A request with no timeout uses the client's default timeout."""
        client, session, _ = _client([_Resp(body={})], timeout=12.0)
        client.get("http://x")
        assert session.calls[0][2]["timeout"] == 12.0

    def test_get_timeout_override(self):
        """A per-request timeout overrides the client default."""
        client, session, _ = _client([_Resp(body={})], timeout=12.0)
        client.get("http://x", timeout=3.0)
        assert session.calls[0][2]["timeout"] == 3.0

    def test_post_dispatches_to_session_post(self):
        """post() routes to the session's post verb."""
        client, session, _ = _client([_Resp(body={})])
        client.post("http://x", data={"a": 1})
        assert session.calls[0][0] == "POST"

    def test_get_json_decodes_body(self):
        """get_json returns the decoded JSON body."""
        client, _, _ = _client([_Resp(body={"results": [1, 2]})])
        assert client.get_json("http://x") == {"results": [1, 2]}

    def test_send_falls_back_to_request(self):
        """A session without a verb method is driven via request()."""
        session = _RequestOnlySession(_Resp(body={"ok": 1}))
        client = HttpClient(session=session)
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls[0][0] == "GET"


@pytest.mark.unit
class TestRetry:
    """The Retry-After-aware retry/back-off loop."""

    def test_retry_after_is_honoured(self):
        """A retryable status waits the Retry-After seconds, then succeeds."""
        client, session, waits = _client(
            [_Resp(status=429, headers={"Retry-After": "7"}), _Resp(body={"ok": 1})]
        )
        assert client.get_json("http://x") == {"ok": 1}
        assert waits == [7.0]
        assert len(session.calls) == 2

    def test_backoff_maths_without_retry_after(self):
        """No Retry-After backs off as backoff_factor * 2**attempt."""
        client, _, waits = _client(
            [_Resp(status=500), _Resp(status=500), _Resp(body={"ok": 1})],
            backoff_factor=3.0,
        )
        client.get("http://x")
        assert waits == [3.0, 6.0]

    def test_default_forcelist_retries_500(self):
        """A 500 is retried under the default forcelist."""
        client, session, _ = _client([_Resp(status=500), _Resp(body={"ok": 1})])
        client.get("http://x")
        assert len(session.calls) == 2

    def test_exhaustion_raises(self):
        """Exhausted retries raise the final response's HTTPError."""
        client, session, waits = _client(
            [_Resp(status=429, headers={"Retry-After": "0"})] * 4, max_retries=2
        )
        with pytest.raises(requests.HTTPError):
            client.get("http://x")
        assert len(waits) == 2
        assert len(session.calls) == 3

    def test_status_outside_forcelist_raises_immediately(self):
        """A status not in the forcelist raises without retrying."""
        client, session, _ = _client([_Resp(status=500)], status_forcelist=(429,))
        with pytest.raises(requests.HTTPError):
            client.get("http://x")
        assert len(session.calls) == 1


@pytest.mark.unit
class TestDownload:
    """Streamed download to disk."""

    def test_download_writes_bytes_and_returns_path(self, tmp_path):
        """download streams the blocks to dest and returns the path."""
        payload = b"earthlens" * 50
        session = _RecordingSession(
            [_Resp(headers={"Content-Length": str(len(payload))}, blocks=[payload])]
        )
        client = HttpClient(session=session)
        dest = tmp_path / "nested" / "out.bin"
        result = client.download("http://x/file", dest, progress=False)
        assert result == dest
        assert dest.read_bytes() == payload

    def test_download_requests_stream(self, tmp_path):
        """download issues a streaming GET (stream=True)."""
        session = _RecordingSession([_Resp(blocks=[b"a"])])
        HttpClient(session=session).download(
            "http://x", tmp_path / "f", progress=False
        )
        assert session.calls[0][2]["stream"] is True

    def test_download_closes_response(self, tmp_path):
        """download closes the streaming response when finished."""
        response = _Resp(blocks=[b"a", b"b"])
        session = _RecordingSession([response])
        HttpClient(session=session).download(
            "http://x", tmp_path / "f", progress=False
        )
        assert response.closed is True

    def test_download_without_content_length(self, tmp_path):
        """A missing Content-Length still writes every block."""
        session = _RecordingSession([_Resp(blocks=[b"x", b"y", b"z"])])
        dest = tmp_path / "f"
        HttpClient(session=session).download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"xyz"

    def test_download_skips_empty_keepalive_chunks(self, tmp_path):
        """Empty keep-alive chunks are skipped, real blocks written."""
        session = _RecordingSession([_Resp(blocks=[b"", b"data", b""])])
        dest = tmp_path / "f"
        HttpClient(session=session).download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"data"

    def test_download_retries_before_streaming(self, tmp_path):
        """A retryable status on the initial response is retried."""
        session = _RecordingSession(
            [_Resp(status=503, headers={"Retry-After": "0"}), _Resp(blocks=[b"ok"])]
        )
        client = HttpClient(session=session, sleep=lambda _: None)
        dest = tmp_path / "f"
        client.download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"ok"
        assert len(session.calls) == 2
