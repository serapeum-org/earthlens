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
    _progress_total,
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


class _FlakySession:
    """Raises `exc` on the first `fail_times` calls, then returns `response`."""

    def __init__(self, fail_times: int, exc: BaseException, response: _Resp) -> None:
        self._remaining = fail_times
        self._exc = exc
        self._response = response
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return self._response


class _Clock:
    """Deterministic monotonic clock returning scripted values in order."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0)


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
        [
            ("5", 5.0),
            ("0", 0.0),
            (None, None),
            ("soon", None),
            ("", None),
            ("-1", None),
        ],
    )
    def test_parse(self, value: str | None, expected: float | None):
        """Numeric non-negative values parse; missing/junk/negative yield None."""
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
        assert (
            HttpClient(user_agent="osm-contact/1.0").default_headers["User-Agent"]
            == "osm-contact/1.0"
        )

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

    def test_stream_requests_stream_and_returns_response(self):
        """stream() issues a stream=True GET and returns the open response."""
        resp = _Resp(body={})
        session = _RecordingSession([resp])
        assert HttpClient(session=session).stream("http://x") is resp
        assert session.calls[0][2]["stream"] is True


@pytest.mark.unit
class TestProgressTotal:
    """Progress-bar total derivation from response headers."""

    def test_plain_content_length(self):
        """A numeric Content-Length with no encoding is the total."""
        assert _progress_total({"Content-Length": "1024"}) == 1024

    def test_gzip_encoded_is_unbounded(self):
        """A Content-Encoding body yields no total (compressed length lies)."""
        assert (
            _progress_total({"Content-Length": "1024", "Content-Encoding": "gzip"})
            is None
        )

    def test_missing_or_non_numeric_is_none(self):
        """Absent or non-numeric Content-Length yields no total."""
        assert _progress_total({}) is None
        assert _progress_total({"Content-Length": "big"}) is None

    def test_identity_encoding_is_treated_as_unencoded(self):
        """Content-Encoding: identity is a no-op, so Content-Length is trusted."""
        assert (
            _progress_total({"Content-Length": "512", "Content-Encoding": "identity"})
            == 512
        )


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

    def test_retry_after_is_capped_by_max_backoff(self):
        """A large Retry-After is clamped to max_backoff, not honoured raw."""
        client, _, waits = _client(
            [_Resp(status=429, headers={"Retry-After": "86400"}), _Resp(body={})],
            max_backoff=30.0,
        )
        client.get("http://x")
        assert waits == [30.0]

    def test_max_backoff_none_leaves_wait_uncapped(self):
        """max_backoff=None honours the raw Retry-After wait."""
        client, _, waits = _client(
            [_Resp(status=429, headers={"Retry-After": "120"}), _Resp(body={})],
            max_backoff=None,
        )
        client.get("http://x")
        assert waits == [120.0]

    def test_negative_retry_after_falls_back_to_backoff(self):
        """A negative Retry-After is ignored and never reaches sleep(negative)."""
        client, _, waits = _client(
            [_Resp(status=429, headers={"Retry-After": "-1"}), _Resp(body={})],
            backoff_factor=2.0,
        )
        client.get("http://x")
        assert waits == [2.0]

    def test_retried_response_is_closed(self):
        """A retried response is closed before the next attempt (no leak)."""
        first = _Resp(status=503, headers={"Retry-After": "0"})
        session = _RecordingSession([first, _Resp(body={"ok": 1})])
        HttpClient(session=session, sleep=lambda _: None).get("http://x")
        assert first.closed is True

    def test_errored_response_is_closed(self):
        """The final errored response is closed before the error propagates."""
        errored = _Resp(status=404)
        session = _RecordingSession([errored])
        with pytest.raises(requests.HTTPError):
            HttpClient(session=session).get("http://x")
        assert errored.closed is True


@pytest.mark.unit
class TestRetryOnExceptions:
    """Retrying on configured transport exceptions."""

    def test_retries_configured_exception_then_succeeds(self):
        """A configured exception is retried with back-off, then succeeds."""
        session = _FlakySession(
            2, requests.ConnectionError("boom"), _Resp(body={"ok": 1})
        )
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            backoff_factor=2.0,
            retry_on_exceptions=(requests.ConnectionError,),
        )
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls == 3
        assert waits == [2.0, 4.0]

    def test_unconfigured_exception_propagates_immediately(self):
        """An exception not in retry_on_exceptions is not retried."""
        session = _FlakySession(1, requests.ConnectionError("boom"), _Resp(body={}))
        client = HttpClient(session=session)
        with pytest.raises(requests.ConnectionError):
            client.get("http://x")
        assert session.calls == 1

    def test_exhausted_exception_reraises(self):
        """A persistent configured exception re-raises after the budget."""
        session = _FlakySession(10, requests.Timeout("t"), _Resp(body={}))
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            max_retries=2,
            retry_on_exceptions=(requests.Timeout,),
        )
        with pytest.raises(requests.Timeout):
            client.get("http://x")
        assert session.calls == 3
        assert len(waits) == 2


@pytest.mark.unit
class TestRetryPredicate:
    """Retrying a response the predicate marks retryable."""

    def test_predicate_retries_a_2xx_then_returns(self):
        """A 200 the predicate flags is retried until it clears."""
        session = _RecordingSession(
            [_Resp(body={"status": "quota"}), _Resp(body={"status": "ok"})]
        )
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            retry_predicate=lambda r: r.json().get("status") == "quota",
        )
        assert client.get_json("http://x") == {"status": "ok"}
        assert len(session.calls) == 2

    def test_predicate_exhaustion_returns_last_response(self):
        """When the predicate never clears, the last response is returned."""
        session = _RecordingSession([_Resp(body={"status": "quota"})] * 5)
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            max_retries=2,
            retry_predicate=lambda r: r.json().get("status") == "quota",
        )
        assert client.get("http://x").json() == {"status": "quota"}
        assert len(session.calls) == 3


@pytest.mark.unit
class TestRaiseForStatus:
    """The raise_for_status policy and per-request override."""

    def test_disabled_returns_error_response(self):
        """raise_for_status=False returns a 4xx response unraised."""
        session = _RecordingSession([_Resp(status=404, body={"e": 1})])
        assert (
            HttpClient(session=session, raise_for_status=False)
            .get("http://x")
            .status_code
            == 404
        )

    def test_default_still_raises(self):
        """The default policy raises on an error status."""
        session = _RecordingSession([_Resp(status=400)])
        with pytest.raises(requests.HTTPError):
            HttpClient(session=session).get("http://x")

    def test_per_request_override_disables(self):
        """A per-request raise_for_status=False overrides the client default."""
        session = _RecordingSession([_Resp(status=400, body={})])
        assert (
            HttpClient(session=session)
            .get("http://x", raise_for_status=False)
            .status_code
            == 400
        )


@pytest.mark.unit
class TestThrottle:
    """The min_interval proactive rate limit."""

    def test_min_interval_sleeps_between_requests(self):
        """A second request within the interval sleeps the remainder."""
        session = _RecordingSession([_Resp(body={}), _Resp(body={})])
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            clock=_Clock([0.0, 0.4, 1.0]),
            min_interval=1.0,
        )
        client.get("http://x")
        client.get("http://x")
        assert waits == [pytest.approx(0.6)]

    def test_no_throttle_when_interval_elapsed(self):
        """No sleep when more than the interval has already passed."""
        session = _RecordingSession([_Resp(body={}), _Resp(body={})])
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            clock=_Clock([0.0, 2.0, 2.0]),
            min_interval=1.0,
        )
        client.get("http://x")
        client.get("http://x")
        assert waits == []


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
        HttpClient(session=session).download("http://x", tmp_path / "f", progress=False)
        assert session.calls[0][2]["stream"] is True

    def test_download_closes_response(self, tmp_path):
        """download closes the streaming response when finished."""
        response = _Resp(blocks=[b"a", b"b"])
        session = _RecordingSession([response])
        HttpClient(session=session).download("http://x", tmp_path / "f", progress=False)
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
