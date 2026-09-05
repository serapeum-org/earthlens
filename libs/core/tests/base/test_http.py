"""Unit tests for `earthlens.base.http` (headers, retry, download)."""

from __future__ import annotations

import builtins
import errno
import time
from typing import Any

import pytest
import requests
import urllib3

from earthlens.base.http import (
    DEFAULT_CONNECT_RETRIES,
    DEFAULT_RETRY_EXCEPTIONS,
    DEFAULT_STATUS_FORCELIST,
    DEFAULT_TIMEOUT,
    HttpClient,
    IncompleteDownloadError,
    RangeReadError,
    RequestsGet,
    UnsolicitedPartialContentError,
    _check_magic,
    _default_user_agent,
    _is_local_storage_error,
    _parse_content_range,
    _parse_retry_after,
    _progress_total,
    _strong_etag,
    classify_transport_error,
    is_network_unreachable,
    prefer_ipv4,
    redact_url,
    retry_login_forcing_ipv4,
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
        stream_error: BaseException | None = None,
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._blocks = blocks or []
        self._stream_error = stream_error
        self.closed = False

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int | None = None) -> Any:
        yield from self._blocks
        if self._stream_error is not None:
            raise self._stream_error

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

    def post(self, url: str, **kwargs: Any) -> _Resp:
        return self.get(url, **kwargs)


def _read_reset() -> requests.ConnectionError:
    """A connection error that carries a mid-response reset as its cause."""
    return requests.ConnectionError(ConnectionResetError("connection reset by peer"))


def _connect_refused() -> requests.ConnectionError:
    """A connection error that carries a refused connect as its cause."""
    return requests.ConnectionError(ConnectionRefusedError("connection refused"))


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

    def test_future_http_date_yields_positive_delay(self):
        """A future HTTP-date Retry-After parses to a positive delay."""
        assert _parse_retry_after("Fri, 31 Dec 2099 23:59:59 GMT") > 0

    def test_past_http_date_clamps_to_zero(self):
        """A past HTTP-date never sleeps backwards — it clamps to zero."""
        assert _parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT") == 0.0

    def test_http_date_parsing_none_result_is_none(self, monkeypatch):
        """A parsedate result of None yields None (defensive guard)."""
        monkeypatch.setattr(
            "earthlens.base.http.parsedate_to_datetime", lambda value: None
        )
        assert _parse_retry_after("not-a-real-date") is None


@pytest.mark.unit
class TestRedactUrl:
    """URL redaction for retry logs (secrets can ride in path or query)."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://firms.example/api/SECRETKEY/area?x=1", "https://firms.example"),
            ("https://host/p?api_key=SECRET", "https://host"),
            ("https://user:SECRET@host/p", "https://host"),
            ("not-a-url", "<url>"),
            ("", "<url>"),
        ],
    )
    def test_redact(self, url: str, expected: str):
        """Path, query, and userinfo are stripped to scheme://host."""
        assert redact_url(url) == expected

    def test_retry_log_omits_url_path_and_query(self):
        """A retry warning logs only the host — never a path/query secret."""
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            session = _RecordingSession(
                [_Resp(status=429, headers={"Retry-After": "0"}), _Resp(body={})]
            )
            HttpClient(session=session, sleep=lambda _: None).get(
                "https://h/api/SECRETKEY/x?api_key=TOPSECRET"
            )
        finally:
            logger.remove(sink)
        joined = " ".join(messages)
        assert "SECRETKEY" not in joined
        assert "TOPSECRET" not in joined
        assert "https://h" in joined


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

    def test_default_transport_is_a_pooled_session(self, real_pooled_session):
        """With no session injected, the client pools (ARC-4a).

        The suite's autouse seam swaps the default for the `requests`-module
        adapter so module-level fakes keep working, which would otherwise leave
        the shipped default untested. `real_pooled_session` puts it back.
        """
        import requests

        assert isinstance(HttpClient().session, requests.Session)

    def test_each_client_gets_its_own_pooled_session(self, real_pooled_session):
        """Two clients do not share one session, so their headers cannot collide."""
        first, second = HttpClient(), HttpClient()
        assert first.session is not second.session

    def test_repeated_requests_reuse_one_session(self, real_pooled_session):
        """Pooling is the point: every call goes through the same session object."""
        client = HttpClient(max_retries=0, raise_for_status=False)
        pooled = client.session
        seen: list[str] = []

        def record(url, **_kwargs):
            seen.append(url)
            return _Resp(status=200)

        pooled.get = record

        client.get("https://example.invalid/a")
        client.get("https://example.invalid/b")

        assert client.session is pooled, "the client must not rebuild its session"
        assert seen == [
            "https://example.invalid/a",
            "https://example.invalid/b",
        ], f"both calls should reach the one pooled session; got {seen}"


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

    def test_get_default_tuple_timeout_is_forwarded(self):
        """A client's (connect, read) default reaches the session unchanged."""
        client, session, _ = _client([_Resp(body={})], timeout=(3.05, 27.0))
        client.get("http://x")
        assert session.calls[0][2]["timeout"] == (3.05, 27.0)

    def test_get_tuple_timeout_override_is_forwarded(self):
        """A per-request (connect, read) tuple overrides a scalar default."""
        client, session, _ = _client([_Resp(body={})], timeout=12.0)
        client.get("http://x", timeout=(5.0, 120.0))
        assert session.calls[0][2]["timeout"] == (5.0, 120.0)

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
        """A configured read-phase exception is retried with back-off, then succeeds."""
        session = _FlakySession(2, _read_reset(), _Resp(body={"ok": 1}))
        waits: list[float] = []
        client = HttpClient(
            session=session,
            sleep=waits.append,
            backoff_factor=2.0,
            retry_on_exceptions=(requests.ConnectionError,),
        )
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls == 3, f"expected 3 attempts, got {session.calls}"
        assert waits == [2.0, 4.0], f"unexpected back-off schedule: {waits}"

    def test_unconfigured_exception_propagates_immediately(self):
        """An exception not in retry_on_exceptions is not retried.

        `TooManyRedirects` is outside `DEFAULT_RETRY_EXCEPTIONS` on purpose: a
        redirect loop is a deterministic misconfiguration, so replaying it just
        repeats the loop.
        """
        session = _FlakySession(1, requests.TooManyRedirects("loop"), _Resp(body={}))
        client = HttpClient(session=session)
        with pytest.raises(requests.TooManyRedirects):
            client.get("http://x")
        assert session.calls == 1, f"expected no retry, got {session.calls} attempts"

    def test_read_failures_are_retried_by_default(self):
        """A socket reset mid-response is retried without the caller configuring it."""
        session = _FlakySession(2, _read_reset(), _Resp(body={"ok": 1}))
        client = HttpClient(session=session, sleep=lambda _: None)
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls == 3, f"expected 3 attempts, got {session.calls}"

    def test_connect_failures_get_the_smaller_budget(self):
        """A host that will not accept a connection fails fast, not after six tries.

        The connect budget is deliberately separate: a refused connection rarely
        starts succeeding within a back-off window, so spending the full read
        budget on it turns a clear failure into a slow one.
        """
        session = _FlakySession(9, _connect_refused(), _Resp(body={"ok": 1}))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(requests.ConnectionError):
            client.get("http://x")
        assert session.calls == DEFAULT_CONNECT_RETRIES + 1, (
            f"connect budget should cap attempts at {DEFAULT_CONNECT_RETRIES + 1}, "
            f"got {session.calls}"
        )

    @pytest.mark.parametrize(
        "exc_name", ["SSLError", "ProxyError"], ids=["ssl", "proxy"]
    )
    def test_deterministic_failures_are_never_retried(self, exc_name):
        """A bad certificate or proxy is reproduced exactly, so it is not replayed."""
        exc = getattr(requests.exceptions, exc_name)("nope")
        session = _FlakySession(1, exc, _Resp(body={"ok": 1}))
        client = HttpClient(session=session, sleep=lambda _: None)
        with pytest.raises(requests.exceptions.RequestException):
            client.get("http://x")
        assert session.calls == 1, (
            f"{exc_name} is deterministic and must not be retried; "
            f"got {session.calls} attempts"
        )

    def test_an_explicit_retry_set_is_not_second_guessed(self):
        """A type the caller named is retried even if the classifier would not.

        The classifier decides which budget a failure spends; naming a type in
        `retry_on_exceptions` is already the decision to retry it. Vetoing that
        would silently disable a knob the caller set — as it did for the
        `ContentDecodingError` that risk_indicators retries on purpose.
        """
        session = _FlakySession(
            2, requests.exceptions.ContentDecodingError("gzip"), _Resp(body={"ok": 1})
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            retry_on_exceptions=(requests.exceptions.ContentDecodingError,),
        )
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls == 3, f"expected 3 attempts, got {session.calls}"

    def test_an_explicit_set_overrides_the_never_retry_list(self):
        """`SSLError` is retried when the caller asks for it by name."""
        session = _FlakySession(
            1, requests.exceptions.SSLError("cert"), _Resp(body={"ok": 1})
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            retry_on_exceptions=(requests.exceptions.SSLError,),
        )
        assert client.get_json("http://x") == {"ok": 1}
        assert session.calls == 2, f"expected 2 attempts, got {session.calls}"

    @pytest.mark.parametrize(
        "reason_name, expected",
        [
            ("ReadTimeoutError", "read"),
            ("ProtocolError", "read"),
            ("NewConnectionError", "connect"),
        ],
    )
    def test_a_wrapped_max_retry_error_is_classified_by_its_reason(
        self, reason_name, expected
    ):
        """`MaxRetryError` carries the real failure in `reason`, not in `args`.

        Its own class name matches a connect marker, and connect failures are
        not method-gated — so misreading one as a connect failure would replay
        a `POST` whose request had in fact been delivered.
        """
        import urllib3.exceptions as u3

        from earthlens.base.http import classify_transport_error

        pool = urllib3.HTTPConnectionPool("example.org")
        reasons = {
            "ReadTimeoutError": u3.ReadTimeoutError(pool, "u", "timed out"),
            "ProtocolError": u3.ProtocolError("connection aborted"),
            "NewConnectionError": u3.NewConnectionError(None, "refused"),
        }
        wrapped = requests.ConnectionError(
            u3.MaxRetryError(pool, "http://x", reason=reasons[reason_name])
        )
        assert classify_transport_error(wrapped) == expected, (
            f"{reason_name} should classify as {expected}, "
            f"got {classify_transport_error(wrapped)}"
        )

    def test_policy_selection_does_not_depend_on_object_identity(self):
        """An equal-valued retry tuple behaves the same however it is spelled.

        Deriving the strict policy from `is DEFAULT_RETRY_EXCEPTIONS` made two
        equal tuples behave oppositely, and made `DEFAULT_RETRY_EXCEPTIONS +
        (Extra,)` silently drop the never-retry veto.
        """
        respelled = (
            requests.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )
        as_constant = HttpClient(
            max_retries=5, retry_on_exceptions=DEFAULT_RETRY_EXCEPTIONS
        )
        as_literal = HttpClient(max_retries=5, retry_on_exceptions=respelled)
        assert as_constant.connect_retries == as_literal.connect_retries, (
            "equal retry sets must yield the same connect budget"
        )
        default = HttpClient(max_retries=5)
        assert default.connect_retries == DEFAULT_CONNECT_RETRIES, (
            "the untouched default keeps the small connect budget"
        )

    def test_default_retry_set_excludes_http_error(self):
        """`HTTPError` stays out, so 4xx responses are never replayed."""
        assert not any(
            issubclass(requests.HTTPError, exc) for exc in DEFAULT_RETRY_EXCEPTIONS
        )

    def test_post_is_not_replayed_after_a_read_failure(self):
        """A non-idempotent verb is not replayed: the server may have acted."""
        session = _FlakySession(1, _read_reset(), _Resp(body={"ok": 1}))
        client = HttpClient(session=session, sleep=lambda _: None)
        with pytest.raises(requests.ConnectionError):
            client.post("http://x")
        assert session.calls == 1, f"POST must not replay, got {session.calls} attempts"

    def test_post_is_replayed_after_a_connect_failure(self):
        """A connect failure never reached the server, so replaying it is safe."""
        session = _FlakySession(1, _connect_refused(), _Resp(body={"ok": 1}))
        client = HttpClient(session=session, sleep=lambda _: None)
        client.post("http://x")
        assert session.calls == 2, (
            f"a connect failure is safe to replay for any verb, got {session.calls}"
        )

    def test_post_is_replayed_when_the_caller_opts_in(self):
        """`retry_unsafe_methods=True` is the caller vouching for replay safety."""
        session = _FlakySession(2, _read_reset(), _Resp(body={"ok": 1}))
        client = HttpClient(
            session=session, sleep=lambda _: None, retry_unsafe_methods=True
        )
        client.post("http://x")
        assert session.calls == 3, f"expected 3 attempts, got {session.calls}"

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

    def test_throttle_serialises_concurrent_callers(self):
        """Threads sharing a client take the throttle one at a time.

        Without the lock every thread reads the same `_last_request`, decides
        the interval has elapsed, and they all fire at once — the burst the
        rate limit exists to prevent.
        """
        import threading

        overlaps: list[int] = []
        inside = 0
        guard = threading.Lock()

        def slow_sleep(seconds: float) -> None:
            nonlocal inside
            with guard:
                inside += 1
                overlaps.append(inside)
            time.sleep(0.01)
            with guard:
                inside -= 1

        client = HttpClient(
            session=_RecordingSession([]),
            sleep=slow_sleep,
            clock=lambda: 0.0,
            min_interval=1.0,
        )
        client._last_request = 0.0
        threads = [threading.Thread(target=client._throttle) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert max(overlaps) == 1, f"throttle ran concurrently: {overlaps}"
        assert len(overlaps) == 8, f"every caller should throttle: {overlaps}"

    def test_zero_interval_never_sleeps_or_records(self):
        """The default (no throttle) is a no-op, not a zero-length wait.

        Asserts the observable behaviour rather than sabotaging
        `_throttle_lock` to prove the short-circuit: replacing the lock with
        `None` tests an internal that no caller can reach.
        """
        waits: list[float] = []
        client = HttpClient(session=_RecordingSession([]), sleep=waits.append)
        client._throttle()
        client._throttle()
        assert waits == [], "min_interval=0 must not sleep"
        assert client._last_request is None, (
            "with no throttle configured there is nothing to record"
        )


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

    def test_download_forwards_tuple_timeout(self, tmp_path):
        """download forwards a (connect, read) timeout pair to the streaming GET."""
        session = _RecordingSession([_Resp(blocks=[b"ok"])])
        HttpClient(session=session).download(
            "http://x", tmp_path / "f", progress=False, timeout=(5.0, 120.0)
        )
        assert session.calls[0][2]["timeout"] == (5.0, 120.0)

    def test_download_uses_default_tuple_timeout(self, tmp_path):
        """download streams with the client's default tuple when none is given."""
        session = _RecordingSession([_Resp(blocks=[b"ok"])])
        HttpClient(session=session, timeout=(3.05, 60.0)).download(
            "http://x", tmp_path / "f", progress=False
        )
        assert session.calls[0][2]["timeout"] == (3.05, 60.0)

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

    def test_download_is_atomic_no_part_left(self, tmp_path):
        """A successful download renames the temp and leaves no .part."""
        session = _RecordingSession([_Resp(blocks=[b"data"])])
        dest = tmp_path / "out.bin"
        HttpClient(session=session).download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"data"
        assert not dest.with_name("out.bin.part").exists()

    def test_download_cleans_partial_on_stream_failure(self, tmp_path):
        """A mid-stream failure removes the temp and leaves no dest."""
        session = _RecordingSession(
            [_Resp(blocks=[b"partial"], stream_error=OSError("mid-stream"))]
        )
        dest = tmp_path / "out.bin"
        client = HttpClient(session=session)
        with pytest.raises(OSError):
            client.download("http://x", dest, progress=False)
        assert not dest.exists()
        assert not dest.with_name("out.bin.part").exists()

    def test_non_atomic_failure_does_not_delete_dest(self, tmp_path):
        """A failed atomic=False download leaves dest present, though truncated."""
        session = _RecordingSession(
            [_Resp(blocks=[b"partial"], stream_error=OSError("mid-stream"))]
        )
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"previously downloaded")
        client = HttpClient(session=session)
        with pytest.raises(OSError):
            client.download("http://x", dest, progress=False, atomic=False)
        assert dest.exists()
        # Documented consequence of atomic=False: the stream opens dest "wb", so
        # the old contents are already gone before any failure. The failure path
        # only declines to delete it on top of that.
        assert dest.read_bytes() == b"partial"

    def test_atomic_default_protects_an_existing_dest(self, tmp_path):
        """The atomic default is what actually preserves the previous contents."""
        session = _RecordingSession(
            [_Resp(blocks=[b"partial"], stream_error=OSError("mid-stream"))]
        )
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"previously downloaded")
        client = HttpClient(session=session)
        with pytest.raises(OSError):
            client.download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"previously downloaded"

    def test_non_atomic_retry_failure_keeps_existing_dest(self, tmp_path):
        """An exhausted retry on atomic=False does not delete the old dest."""
        session = _FlakySession(
            10, requests.ConnectionError("boom"), _Resp(blocks=[b"ok"])
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            retry_on_exceptions=(requests.ConnectionError,),
        )
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"previously downloaded")
        with pytest.raises(requests.ConnectionError):
            client.download("http://x", dest, progress=False, atomic=False)
        assert dest.read_bytes() == b"previously downloaded"

    def test_atomic_failure_still_removes_part(self, tmp_path):
        """The atomic path keeps its promise: the .part temp is cleaned up."""
        session = _RecordingSession(
            [_Resp(blocks=[b"partial"], stream_error=OSError("mid-stream"))]
        )
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"previously downloaded")
        client = HttpClient(session=session)
        with pytest.raises(OSError):
            client.download("http://x", dest, progress=False)
        assert not dest.with_name("out.bin.part").exists()
        assert dest.read_bytes() == b"previously downloaded"

    def test_non_atomic_success_overwrites_dest(self, tmp_path):
        """A successful atomic=False download still replaces the old contents."""
        session = _RecordingSession([_Resp(blocks=[b"fresh"])])
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"stale")
        HttpClient(session=session).download(
            "http://x", dest, progress=False, atomic=False
        )
        assert dest.read_bytes() == b"fresh"

    def test_download_retries_on_exception_then_succeeds(self, tmp_path):
        """A configured transport exception retries the whole download."""
        session = _FlakySession(
            2, requests.ConnectionError("boom"), _Resp(blocks=[b"ok"])
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            retry_on_exceptions=(requests.ConnectionError,),
        )
        dest = tmp_path / "out.bin"
        client.download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"ok"
        assert session.calls == 3
        assert not dest.with_name("out.bin.part").exists()

    def test_download_exhausts_exception_retries_and_reraises(self, tmp_path):
        """A persistent transport exception re-raises after the budget."""
        session = _FlakySession(
            10, requests.ConnectionError("boom"), _Resp(blocks=[b"ok"])
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            max_retries=2,
            retry_on_exceptions=(requests.ConnectionError,),
        )
        dest = tmp_path / "out.bin"
        with pytest.raises(requests.ConnectionError):
            client.download("http://x", dest, progress=False)
        assert session.calls == 3
        assert not dest.with_name("out.bin.part").exists()

    def test_download_cleans_temp_when_rename_fails(self, tmp_path):
        """A failed atomic rename still removes the .part temp."""
        session = _RecordingSession([_Resp(blocks=[b"data"])])
        dest = tmp_path / "out.bin"
        dest.mkdir()  # a directory target makes tmp.replace(dest) fail
        client = HttpClient(session=session)
        with pytest.raises(OSError):
            client.download("http://x", dest, progress=False)
        assert not dest.with_name("out.bin.part").exists()

    def test_download_retries_on_predicate(self, tmp_path):
        """download retries a response the predicate flags before streaming."""
        session = _RecordingSession(
            [_Resp(body={"retry": True}), _Resp(blocks=[b"ok"])]
        )
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            retry_predicate=lambda r: r.json() == {"retry": True},
        )
        dest = tmp_path / "f"
        client.download("http://x", dest, progress=False)
        assert dest.read_bytes() == b"ok"
        assert len(session.calls) == 2

    def test_download_non_atomic_writes_dest_directly(self, tmp_path):
        """atomic=False streams straight to dest with no temp file."""
        session = _RecordingSession([_Resp(blocks=[b"data"])])
        dest = tmp_path / "out.bin"
        HttpClient(session=session).download(
            "http://x", dest, progress=False, atomic=False
        )
        assert dest.read_bytes() == b"data"
        assert not dest.with_name("out.bin.part").exists()


class TestCheckMagic:
    """The leading-bytes guard that rejects an error page served as a file."""

    def test_accepts_matching_single_prefix(self, tmp_path):
        """A body starting with the one expected prefix passes silently."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"CDF\x01payload")
        assert _check_magic(path, b"CDF", "http://host/grid.nc") is None

    def test_accepts_any_of_several_prefixes(self, tmp_path):
        """With a tuple of prefixes, matching any one is enough."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"\x89HDF\r\n\x1a\npayload")
        assert _check_magic(path, (b"CDF", b"\x89HDF"), "http://host/g.nc") is None

    def test_rejects_html_error_page(self, tmp_path):
        """An HTML body served under a .nc name raises ValueError."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"<html>Error 500</html>")
        with pytest.raises(ValueError, match="does not start with") as exc:
            _check_magic(path, b"CDF", "http://host/grid.nc")
        assert "<html>" in str(exc.value), (
            f"message should show what arrived: {exc.value}"
        )

    def test_message_reports_size_and_head(self, tmp_path):
        """The message carries the byte count and the first 24 bytes seen."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"x" * 100)
        with pytest.raises(ValueError) as exc:
            _check_magic(path, b"CDF", "http://host/grid.nc")
        message = str(exc.value)
        assert "100 bytes" in message, f"size missing from: {message}"
        assert repr(b"x" * 24) in message, f"head missing from: {message}"

    def test_message_redacts_the_url(self, tmp_path):
        """Only scheme://host reaches the message, never a URL-borne secret."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"nope")
        with pytest.raises(ValueError) as exc:
            _check_magic(path, b"CDF", "https://host/api?token=SECRET")
        message = str(exc.value)
        assert "SECRET" not in message, f"secret leaked into: {message}"
        assert "https://host" in message, f"host missing from: {message}"

    def test_short_body_shorter_than_prefix_is_rejected(self, tmp_path):
        """A truncated body too short to hold the prefix still raises."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"CD")
        with pytest.raises(ValueError, match="does not start with"):
            _check_magic(path, b"CDF", "http://host/grid.nc")

    def test_empty_body_is_rejected(self, tmp_path):
        """A zero-byte body raises rather than passing as a valid file."""
        path = tmp_path / "grid.nc"
        path.write_bytes(b"")
        with pytest.raises(ValueError, match="0 bytes"):
            _check_magic(path, b"CDF", "http://host/grid.nc")


class TestDownloadExpectMagic:
    """`download(expect_magic=...)` validates the body before publishing it."""

    def test_matching_body_is_published(self, tmp_path):
        """A body with the expected prefix lands at dest as usual."""
        session = _RecordingSession([_Resp(blocks=[b"CDF\x01", b"payload"])])
        dest = tmp_path / "out.nc"
        result = HttpClient(session=session).download(
            "http://x/out.nc", dest, progress=False, expect_magic=b"CDF"
        )
        assert result == dest
        assert dest.read_bytes() == b"CDF\x01payload"

    def test_wrong_body_raises_and_leaves_no_dest(self, tmp_path):
        """A non-matching body raises ValueError and never becomes dest."""
        session = _RecordingSession([_Resp(blocks=[b"<html>error</html>"])])
        dest = tmp_path / "out.nc"
        client = HttpClient(session=session)
        with pytest.raises(ValueError, match="does not start with"):
            client.download(
                "http://x/out.nc", dest, progress=False, expect_magic=b"CDF"
            )
        assert not dest.exists(), "an error page must not be published as dest"
        assert not dest.with_name("out.nc.part").exists(), "temp must be cleaned"

    def test_wrong_body_keeps_a_previous_dest(self, tmp_path):
        """A rejected re-download leaves the previously good dest intact."""
        session = _RecordingSession([_Resp(blocks=[b"<html>error</html>"])])
        dest = tmp_path / "out.nc"
        dest.write_bytes(b"CDF\x01earlier good file")
        client = HttpClient(session=session)
        with pytest.raises(ValueError):
            client.download(
                "http://x/out.nc", dest, progress=False, expect_magic=b"CDF"
            )
        assert dest.read_bytes() == b"CDF\x01earlier good file"

    def test_omitting_expect_magic_skips_the_check(self, tmp_path):
        """Without expect_magic any body is written, preserving the old contract."""
        session = _RecordingSession([_Resp(blocks=[b"<html>error</html>"])])
        dest = tmp_path / "out.nc"
        HttpClient(session=session).download("http://x/out.nc", dest, progress=False)
        assert dest.read_bytes() == b"<html>error</html>"

    def test_response_is_closed_when_magic_fails(self, tmp_path):
        """The rejected response is still released, not left open."""
        response = _Resp(blocks=[b"<html>"])
        session = _RecordingSession([response])
        client = HttpClient(session=session)
        with pytest.raises(ValueError):
            client.download(
                "http://x/out.nc",
                tmp_path / "out.nc",
                progress=False,
                expect_magic=b"CDF",
            )
        assert response.closed, "the response must be closed even on rejection"


class _Capture:
    """Callable that records the kwargs of the requests call it stands in for."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def __call__(self, url: str, **kwargs: Any) -> _Resp:
        self.kwargs = kwargs
        return _Resp(body={})


class TestRequestsGet:
    """Tests for the RequestsGet session shim's default-timeout guarantee."""

    def test_get_applies_default_timeout_when_omitted(self, monkeypatch):
        """A get with no timeout kwarg forwards DEFAULT_TIMEOUT."""
        capture = _Capture()
        monkeypatch.setattr(requests, "get", capture)
        RequestsGet().get("https://x/y")
        assert capture.kwargs["timeout"] == DEFAULT_TIMEOUT

    def test_get_leaves_explicit_timeout(self, monkeypatch):
        """An explicit timeout is forwarded unchanged."""
        capture = _Capture()
        monkeypatch.setattr(requests, "get", capture)
        RequestsGet().get("https://x/y", timeout=5.0)
        assert capture.kwargs["timeout"] == 5.0

    def test_get_leaves_explicit_none_timeout(self, monkeypatch):
        """An explicit timeout of None is left as None, not overridden."""
        capture = _Capture()
        monkeypatch.setattr(requests, "get", capture)
        RequestsGet().get("https://x/y", timeout=None)
        assert capture.kwargs["timeout"] is None

    def test_post_applies_default_timeout_when_omitted(self, monkeypatch):
        """A post with no timeout kwarg forwards DEFAULT_TIMEOUT."""
        capture = _Capture()
        monkeypatch.setattr(requests, "post", capture)
        RequestsGet().post("https://x/y")
        assert capture.kwargs["timeout"] == DEFAULT_TIMEOUT


@pytest.mark.unit
class TestThreadLocalSession:
    """`thread_local_session` pools per thread without sharing across threads."""

    def test_same_thread_and_key_reuses_one_session(self, real_pooled_session):
        """Repeated calls hand back the same object — that is the pooling."""
        from earthlens.base.http import thread_local_session

        assert thread_local_session("demo") is thread_local_session("demo")

    def test_different_keys_do_not_share_a_session(self, real_pooled_session):
        """Two providers keep separate cookie jars and headers."""
        from earthlens.base.http import thread_local_session

        assert thread_local_session("a") is not thread_local_session("b")

    def test_each_thread_gets_its_own_session(self, real_pooled_session):
        """`requests.Session` is not guaranteed thread-safe, so none is shared."""
        import threading

        from earthlens.base.http import thread_local_session

        seen: dict[str, object] = {}

        def grab(name):
            seen[name] = thread_local_session("demo")

        workers = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert len(seen) == 4
        assert len({id(session) for session in seen.values()}) == 4, (
            "each thread must get its own session, got "
            f"{len({id(s) for s in seen.values()})} distinct for 4 threads"
        )

    def test_reset_forces_a_rebuild(self, real_pooled_session):
        """Clearing the cache makes the next call build against the current transport."""
        from earthlens.base.http import (
            reset_thread_local_sessions,
            thread_local_session,
        )

        first = thread_local_session("demo")
        reset_thread_local_sessions()

        assert thread_local_session("demo") is not first


class TestPreferIpv4:
    """`prefer_ipv4` narrows urllib3 to IPv4 without ever restoring IPv6."""

    @pytest.fixture(autouse=True)
    def _restore_has_ipv6(self):
        """Save and restore the process-global HAS_IPV6 around each test."""
        import urllib3.util.connection as connection

        saved = connection.HAS_IPV6
        try:
            yield
        finally:
            connection.HAS_IPV6 = saved

    def test_sets_has_ipv6_false(self):
        """It flips the urllib3 flag so getaddrinfo is asked for IPv4 only."""
        import urllib3.util.connection as connection

        connection.HAS_IPV6 = True
        prefer_ipv4()
        assert connection.HAS_IPV6 is False

    def test_is_idempotent(self):
        """A second call leaves the flag false rather than toggling it back."""
        import urllib3.util.connection as connection

        prefer_ipv4()
        prefer_ipv4()
        assert connection.HAS_IPV6 is False

    def test_allowed_gai_family_becomes_af_inet(self):
        """urllib3 then resolves connections in the IPv4 address family."""
        import socket

        import urllib3.util.connection as connection

        prefer_ipv4()
        assert connection.allowed_gai_family() == socket.AF_INET


def _enetunreach_connection_error() -> requests.ConnectionError:
    """Build a ConnectionError shaped like a real dead-IPv6-route failure."""
    return requests.ConnectionError(
        "HTTPSConnectionPool(host='urs.earthdata.nasa.gov', port=443): Max "
        "retries exceeded (Caused by NewConnectionError('Failed to establish a "
        f"new connection: [Errno {errno.ENETUNREACH}] Network is unreachable'))"
    )


class TestIsNetworkUnreachable:
    """`is_network_unreachable` detects ENETUNREACH anywhere in the chain."""

    def test_bare_enetunreach_oserror_is_detected(self):
        """An OSError whose errno is ENETUNREACH is recognised directly."""
        assert is_network_unreachable(OSError(errno.ENETUNREACH, "unreachable"))

    def test_wrapped_message_is_detected(self):
        """A requests error that only embeds the errno as text is recognised."""
        assert is_network_unreachable(_enetunreach_connection_error())

    def test_chained_oserror_is_detected(self):
        """An ENETUNREACH reached only through __cause__ is found."""
        top = RuntimeError("wrapper")
        top.__cause__ = OSError(errno.ENETUNREACH, "unreachable")
        assert is_network_unreachable(top)

    def test_unrelated_error_is_not_detected(self):
        """A different failure (e.g. connection reset) is not ENETUNREACH."""
        assert not is_network_unreachable(requests.ConnectionError("reset by peer"))

    def test_none_is_not_detected(self):
        """A missing exception is reported as not unreachable."""
        assert not is_network_unreachable(None)

    def test_a_reference_cycle_terminates(self):
        """A cyclic cause/context chain does not loop forever."""
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert not is_network_unreachable(a)


class TestRetryLoginForcingIpv4:
    """`retry_login_forcing_ipv4` forces IPv4 only on an observed ENETUNREACH."""

    @pytest.fixture(autouse=True)
    def _restore_has_ipv6(self):
        """Reset HAS_IPV6 to True before each test and restore it after."""
        import urllib3.util.connection as connection

        saved = connection.HAS_IPV6
        connection.HAS_IPV6 = True
        try:
            yield connection
        finally:
            connection.HAS_IPV6 = saved

    def test_success_first_try_does_not_touch_ipv6(self, _restore_has_ipv6):
        """A login that succeeds runs once and leaves IPv6 enabled."""
        calls = []
        result = retry_login_forcing_ipv4(lambda: calls.append(1) or "ok")
        assert result == "ok"
        assert len(calls) == 1
        assert _restore_has_ipv6.HAS_IPV6 is True

    def test_enetunreach_forces_ipv4_and_retries_once(self, _restore_has_ipv6):
        """An ENETUNREACH flips IPv6 off and the login is retried and returns."""
        calls = {"n": 0}

        def login():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _enetunreach_connection_error()
            return "ok"

        assert retry_login_forcing_ipv4(login) == "ok"
        assert calls["n"] == 2
        assert _restore_has_ipv6.HAS_IPV6 is False

    def test_non_enetunreach_propagates_without_forcing(self, _restore_has_ipv6):
        """A non-ENETUNREACH failure is re-raised and IPv6 is left alone."""
        with pytest.raises(requests.ConnectionError):
            retry_login_forcing_ipv4(
                lambda: (_ for _ in ()).throw(requests.ConnectionError("reset"))
            )
        assert _restore_has_ipv6.HAS_IPV6 is True

    def test_persistent_enetunreach_propagates_after_one_retry(self, _restore_has_ipv6):
        """When the retry also hits ENETUNREACH the error propagates."""
        calls = {"n": 0}

        def login():
            calls["n"] += 1
            raise _enetunreach_connection_error()

        with pytest.raises(requests.ConnectionError):
            retry_login_forcing_ipv4(login)
        assert calls["n"] == 2
        assert _restore_has_ipv6.HAS_IPV6 is False


class _PartialBody:
    """A streaming response that yields `stop_after` bytes then raises.

    `Content-Length` always states the *whole* payload, so a fake that stops
    early is an honest short body — the shape a length post-condition has to
    catch.
    """

    def __init__(self, payload: bytes, stop_after: int | None):
        self.payload = payload
        self.stop_after = stop_after
        self.status_code = 200
        self.headers = {"Content-Length": str(len(payload))}
        self.closed = False

    def iter_content(self, chunk_size: int = 1):
        """Yield the payload in blocks, breaking once `stop_after` is passed."""
        emitted = 0
        for i in range(0, len(self.payload), chunk_size):
            block = self.payload[i : i + chunk_size]
            if self.stop_after is not None and emitted + len(block) > self.stop_after:
                raise requests.ConnectionError(ConnectionResetError("reset"))
            emitted += len(block)
            yield block

    def raise_for_status(self) -> None:
        """No-op: these fixtures only model 2xx bodies."""

    def close(self) -> None:
        self.closed = True


@pytest.mark.unit
class TestTransportClassificationEdges:
    """Edge cases of the connect/read classifier and the resume guards."""

    def test_an_unrecognised_connection_error_is_unknown_not_connect(self):
        """It fails fast on the cheap budget without claiming the request never arrived.

        Calling it `"connect"` would exempt it from the verb gate, because a
        connect failure is replayed for any method on the grounds that the
        server never saw it — which an unidentified failure cannot promise.
        """
        from earthlens.base.http import classify_transport_error

        assert (
            classify_transport_error(requests.ConnectionError("mystery")) == "unknown"
        )

    def test_a_bare_timeout_is_a_read_failure(self):
        """A `Timeout` that is not a `ConnectTimeout` happened after connecting."""
        from earthlens.base.http import classify_transport_error

        assert classify_transport_error(requests.exceptions.Timeout()) == "read"

    def test_a_cycle_in_the_cause_chain_terminates(self):
        """`_causes` must not loop on an exception that references itself."""
        from earthlens.base.http import _causes

        first = requests.ConnectionError("a")
        second = requests.ConnectionError("b")
        first.__cause__ = second
        second.__cause__ = first
        assert len(list(_causes(first))) >= 2, "the walk must yield both and stop"

    def test_an_explicit_connect_retries_overrides_both_defaults(self):
        """The argument wins over the default-set and caller-set resolutions."""
        assert HttpClient(max_retries=5, connect_retries=3).connect_retries == 3


@pytest.mark.unit
class TestUnidentifiedTransportFailures:
    """An unclassifiable failure is cheap to retry but unsafe to replay."""

    def test_it_classifies_as_unknown_rather_than_connect(self):
        """`"connect"` would assert the request never arrived, which is unproven."""
        from earthlens.base.http import classify_transport_error

        assert (
            classify_transport_error(requests.ConnectionError("mystery")) == "unknown"
        )

    def test_a_post_is_not_replayed(self):
        """Only a proven connect failure exempts a non-idempotent verb.

        The conservative choice for the budget — the small connect one — is the
        permissive choice for the verb gate, so the two decisions are made
        separately.
        """
        session = _FlakySession(
            1, requests.ConnectionError("mystery"), _Resp(body={"ok": 1})
        )
        client = HttpClient(session=session, sleep=lambda _: None)
        with pytest.raises(requests.ConnectionError):
            client.post("http://x")
        assert session.calls == 1, f"POST must not replay, got {session.calls}"

    def test_a_get_still_spends_the_cheap_budget(self):
        """An idempotent verb retries, but on the connect budget, not the read one."""
        from earthlens.base.http import DEFAULT_CONNECT_RETRIES

        session = _FlakySession(
            9, requests.ConnectionError("mystery"), _Resp(body={"ok": 1})
        )
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(requests.ConnectionError):
            client.get("http://x")
        assert session.calls == DEFAULT_CONNECT_RETRIES + 1, (
            f"expected the cheap budget, got {session.calls} attempts"
        )


@pytest.mark.unit
class TestRetryOptOutIsHonoured:
    """`max_retries=0` still disables retry after the transport default was armed."""

    def test_zero_max_retries_makes_exactly_one_attempt(self):
        """Nine backends pass `max_retries=0` to opt out; arming a default must not undo that.

        The per-kind connect budget defaults to 1, which would otherwise
        re-enable a retry for a client that asked for none.
        """
        session = _FlakySession(
            9, requests.ConnectionError(ConnectionResetError("r")), _Resp(body={})
        )
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(requests.ConnectionError):
            client.get("http://x")
        assert session.calls == 1, (
            f"max_retries=0 must mean one attempt, got {session.calls}"
        )


@pytest.mark.unit
class TestRemainingRetryBranches:
    """Branches the earlier suites reached only indirectly."""

    def test_a_connect_timeout_classifies_as_connect(self):
        """`ConnectTimeout` is the one unambiguous connect-phase exception."""
        from earthlens.base.http import classify_transport_error

        assert (
            classify_transport_error(requests.exceptions.ConnectTimeout()) == "connect"
        )

    def test_a_read_timeout_classifies_as_read(self):
        """`ReadTimeout` means the connection was made and the response stalled."""
        from earthlens.base.http import classify_transport_error

        assert classify_transport_error(requests.exceptions.ReadTimeout()) == "read"

    def test_a_status_suppression_on_a_post_is_logged(self, caplog):
        """The `5xx`-on-`POST` refusal is visible, matching the transport one."""
        import logging

        from loguru import logger as _loguru

        class _Resp500:
            def __init__(self):
                self.status_code = 500
                self.headers = {}

            def close(self):
                """No-op."""

            def raise_for_status(self):
                """No-op: the caller disabled raising."""

        class _S:
            def __init__(self):
                self.calls = 0

            def post(self, url, **kwargs):
                self.calls += 1
                return _Resp500()

        handler = _loguru.add(
            lambda m: logging.getLogger().debug(m.record["message"]), level="DEBUG"
        )
        try:
            with caplog.at_level(logging.DEBUG):
                session = _S()
                HttpClient(
                    session=session, sleep=lambda _: None, raise_for_status=False
                ).post("http://x")
        finally:
            _loguru.remove(handler)
        assert session.calls == 1, "a POST must not be replayed on a 500"
        assert any("not retrying a POST" in r.message for r in caplog.records), (
            f"the suppression must be logged; got {[r.message for r in caplog.records]}"
        )


@pytest.mark.unit
class TestProgressTotalRejectsUntrustworthyLengths:
    """`_progress_total` is the single source of "how big is this body"."""

    @pytest.mark.parametrize(
        "headers, expected",
        [
            ({"Content-Length": "22"}, 22),
            ({"Content-Length": " 22 "}, 22),
            ({"Content-Length": "22, 22"}, 22),
            ({"Content-Length": "22, 40"}, None),
            ({"Content-Length": "22", "Transfer-Encoding": "chunked"}, None),
            ({"Content-Length": "22", "Transfer-Encoding": "identity"}, 22),
            ({"Content-Length": "22", "Content-Encoding": "gzip"}, None),
            ({"Content-Length": "22", "Content-Encoding": "identity"}, 22),
            ({"Content-Length": "\u00b2\u00b2"}, None),
            ({"Content-Length": ""}, None),
            ({"Content-Length": "22,"}, None),
            ({}, None),
        ],
        ids=[
            "plain",
            "padded",
            "duplicate-agreeing",
            "duplicate-conflicting",
            "chunked-wins",
            "identity-coding-ok",
            "gzip-encoded",
            "identity-encoding-ok",
            "unicode-digits",
            "empty",
            "trailing-comma",
            "absent",
        ],
    )
    def test_only_a_length_describing_the_delivered_bytes_is_used(
        self, headers, expected
    ):
        """A length that will not match the stream is reported as unknown."""
        from earthlens.base.http import _progress_total

        assert _progress_total(headers) == expected

    def test_a_chunked_body_is_unknown_even_with_a_wellformed_length(self):
        """RFC 9110 6.3: a transfer coding makes `Content-Length` non-describing.

        urllib3 frames the body by the chunked encoding, so a length check
        against the header would report a complete body as truncated.
        """
        from earthlens.base.http import _progress_total

        assert (
            _progress_total(
                {"Content-Length": "1048576", "Transfer-Encoding": "chunked"}
            )
            is None
        )


@pytest.mark.unit
class TestDownloadErrorTypes:
    """The two download verdicts land in the right exception families."""

    def test_incomplete_download_is_a_transport_error(self):
        """Providers' error handlers catch `requests` transport errors, so it must be one."""
        from earthlens.base.http import IncompleteDownloadError

        err = IncompleteDownloadError("short body", written=8, expected=22)
        assert isinstance(err, requests.ConnectionError)
        assert isinstance(err, requests.RequestException)
        assert isinstance(err, OSError)

    def test_incomplete_download_carries_both_sizes(self):
        """A caller deciding what to do needs the mismatch, not just the fact of it."""
        from earthlens.base.http import IncompleteDownloadError

        err = IncompleteDownloadError("short body", written=8, expected=22)
        assert (err.written, err.expected) == (8, 22)

    def test_incomplete_download_defaults_its_sizes(self):
        """It stays constructible without them, like any `requests` error."""
        from earthlens.base.http import IncompleteDownloadError

        err = IncompleteDownloadError("boom")
        assert err.written is None
        assert err.expected is None

    def test_an_unsolicited_206_is_a_status_error_not_a_transport_one(self):
        """It must not be swept up by the transport retry set.

        A server that volunteers partial content to a Range-less request does
        not stop doing so, so replaying reproduces the same fragment.
        """
        from earthlens.base.http import (
            DEFAULT_RETRY_EXCEPTIONS,
            UnsolicitedPartialContentError,
        )

        assert issubclass(UnsolicitedPartialContentError, requests.HTTPError)
        assert not issubclass(UnsolicitedPartialContentError, requests.ConnectionError)
        assert not issubclass(UnsolicitedPartialContentError, DEFAULT_RETRY_EXCEPTIONS)

    @pytest.mark.parametrize(
        "name",
        ["IncompleteDownloadError", "UnsolicitedPartialContentError", "RangeReadError"],
    )
    def test_the_error_types_are_exported(self, name):
        """Both new names, and the existing one, reach callers from `earthlens.base`."""
        import earthlens.base as base

        assert hasattr(base, name), f"{name} is not importable from earthlens.base"
        assert name in base.__all__, f"{name} is missing from __all__"


@pytest.mark.unit
class TestAcceptEncodingProvenance:
    """Whether the caller chose `Accept-Encoding`, or it is the client's default."""

    @pytest.mark.parametrize(
        "headers, explicit",
        [
            (None, False),
            ({"X-Api-Key": "k"}, False),
            ({"Accept-Encoding": "gzip"}, True),
            ({"accept-encoding": "gzip"}, True),
            ({"ACCEPT-ENCODING": "br"}, True),
        ],
        ids=["none", "unrelated", "canonical", "lowercase", "uppercase"],
    )
    def test_the_flag_records_who_set_it(self, headers, explicit):
        """`_default_headers` cannot answer this after the merge, so it is recorded."""
        assert HttpClient(headers=headers)._accept_encoding_is_explicit is explicit

    def test_the_clients_own_default_is_still_stamped(self):
        """Recording provenance must not change what is actually sent."""
        client = HttpClient()
        assert client.default_headers["Accept-Encoding"] == "gzip, deflate"

    def test_a_callers_choice_still_wins_the_header(self):
        """The flag is additional information, not a replacement for the merge."""
        client = HttpClient(headers={"Accept-Encoding": "identity"})
        assert client.default_headers["Accept-Encoding"] == "identity"


# ---------------------------------------------------------------------------
# `download` invariants.
#
# The fakes below stand in for the transport, so shapes urllib3 would reject on
# a real socket are reachable here on purpose: what is pinned is `download`'s
# own guarantee, not urllib3's.
# ---------------------------------------------------------------------------

_PAYLOAD = b"0123456789ABCDEFGHIJKL"  # 22 bytes


class _ScriptedBody:
    """One response: a status, headers, and a body that may break part-way."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        advertised: int | None = None,
        stop_after: int | None = None,
        break_at_end: bool = False,
        omit_length: bool = False,
    ) -> None:
        self.payload = payload
        self.status_code = status
        self.stop_after = stop_after
        self.break_at_end = break_at_end
        self.headers: dict[str, str] = {}
        if not omit_length:
            length = len(payload) if advertised is None else advertised
            self.headers["Content-Length"] = str(length)
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Yield the payload, optionally breaking mid-way or after the last byte."""
        emitted = 0
        for i in range(0, len(self.payload), chunk_size):
            block = self.payload[i : i + chunk_size]
            if self.stop_after is not None and emitted + len(block) > self.stop_after:
                raise _read_reset()
            emitted += len(block)
            yield block
        if self.break_at_end:
            raise _read_reset()

    def raise_for_status(self) -> None:
        """Raise for a 4xx/5xx, as `requests` does."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        """Record that the response was released."""
        self.closed = True


def _clone_body(body: _ScriptedBody) -> _ScriptedBody:
    """Copy a scripted response, since a body can only be consumed once."""
    clone = _ScriptedBody(body.payload, status=body.status_code)
    clone.headers = dict(body.headers)
    clone.stop_after = body.stop_after
    clone.break_at_end = body.break_at_end
    return clone


class _ScriptedSession:
    """Serves scripted responses in order, repeating the last one, and records requests."""

    def __init__(self, *responses: _ScriptedBody) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: Any) -> _ScriptedBody:
        """Return the next scripted response, recording the request shape."""
        self.requests.append((url, dict(kwargs.get("headers") or {})))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return _clone_body(self._responses[index])

    @property
    def calls(self) -> int:
        """How many requests were issued."""
        return len(self.requests)


class _ConnectThenGood:
    """Fails to connect once, then serves the object."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _ScriptedBody:
        """Raise a refused connect on the first call, then return the body."""
        self.calls += 1
        if self.calls == 1:
            raise _connect_refused()
        return _ScriptedBody(_PAYLOAD)


class _AlwaysRaises:
    """Raises the same transport error on every request."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        """Record the attempt and raise."""
        self.calls += 1
        raise self._exc


class _RecordingBody:
    """Wraps a scripted body, logging its release into a shared event list."""

    def __init__(self, inner: _ScriptedBody, events: list[str]) -> None:
        self._inner = inner
        self._events = events
        self.status_code = inner.status_code
        self.headers = inner.headers

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Delegate to the wrapped body."""
        return self._inner.iter_content(chunk_size)

    def raise_for_status(self) -> None:
        """Delegate to the wrapped body."""
        self._inner.raise_for_status()

    def close(self) -> None:
        """Log the release, then delegate."""
        self._events.append("close")
        self._inner.close()


class _ClosingSession:
    """Serves a 503 then a 200, logging closes into a shared event list."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _RecordingBody:
        """Return a retryable status first, then the object."""
        self.calls += 1
        body = (
            _ScriptedBody(b"", status=503)
            if self.calls == 1
            else _ScriptedBody(_PAYLOAD)
        )
        return _RecordingBody(body, self.events)


class _NullBar:
    """A `tqdm` stand-in that renders nothing."""

    def update(self, n: int) -> None:
        """Ignore progress."""

    def close(self) -> None:
        """Ignore closure."""


class _RecordingTqdm:
    """A `tqdm` stand-in that records the keyword arguments of every bar built."""

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    def __call__(self, *args: Any, **kwargs: Any) -> _NullBar:
        """Record one bar's construction and hand back an inert bar."""
        self.seen.append(kwargs)
        return _NullBar()


def _raise_interrupt(wait: float) -> None:
    """Stand in for a Ctrl-C arriving during a sleep."""
    raise KeyboardInterrupt


@pytest.mark.unit
class TestDownloadRequestShape:
    """What `download` puts on the wire, and that every attempt puts the same thing."""

    def test_identity_is_the_default_encoding(self, tmp_path):
        """The length check compares against delivered bytes, so nothing is encoded."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g", tmp_path / "g", progress=False
        )
        assert session.requests[0][1]["Accept-Encoding"] == "identity"

    def test_a_per_call_encoding_wins(self, tmp_path):
        """A backend protecting its own magic check keeps the encoding it asked for."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g",
            tmp_path / "g",
            progress=False,
            headers={"Accept-Encoding": "gzip"},
        )
        assert session.requests[0][1]["Accept-Encoding"] == "gzip"

    @pytest.mark.parametrize(
        "key", ["Accept-Encoding", "accept-encoding", "ACCEPT-ENCODING"]
    )
    def test_a_constructor_encoding_wins_in_any_casing(self, tmp_path, key):
        """Header names are case-insensitive, so the provenance check must be too."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        HttpClient(
            session=session, sleep=lambda _: None, headers={key: "gzip"}
        ).download("http://x/g", tmp_path / "g", progress=False)
        sent = {name.lower(): value for name, value in session.requests[0][1].items()}
        assert sent["accept-encoding"] == "gzip"

    def test_other_caller_headers_pass_through_verbatim(self, tmp_path):
        """`download` rewrites nothing it did not add."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g",
            tmp_path / "g",
            progress=False,
            headers={"X-Api-Key": "secret", "Range": "bytes=0-5"},
        )
        sent = session.requests[0][1]
        assert sent["X-Api-Key"] == "secret"
        assert sent["Range"] == "bytes=0-5"

    def test_every_attempt_issues_an_identical_request(self, tmp_path):
        """No leg differs from another, because there is no per-attempt header state."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=3)
        with pytest.raises(requests.RequestException):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        shapes = {(url, tuple(sorted(sent.items()))) for url, sent in session.requests}
        assert session.calls > 1, "the fixture must induce more than one attempt"
        assert len(shapes) == 1, f"attempts differed: {shapes}"


@pytest.mark.unit
class TestDownloadWritesWholeObjects:
    """Every attempt truncates, and a body that misses its length never publishes."""

    def test_a_stale_part_file_is_truncated_not_appended(self, tmp_path):
        """No byte survives from a write this call did not make."""
        dest = tmp_path / "g"
        (tmp_path / "g.part").write_bytes(b"Z" * 999)
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g", dest, progress=False
        )
        assert dest.read_bytes() == _PAYLOAD

    def test_a_short_body_raises_and_publishes_nothing(self, tmp_path):
        """A body shorter than its own `Content-Length` is never renamed onto `dest`."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], advertised=22))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=2)
        with pytest.raises(IncompleteDownloadError) as excinfo:
            client.download("http://x/g", dest, progress=False)
        assert (excinfo.value.written, excinfo.value.expected) == (8, 22)
        assert not dest.exists()
        assert not (tmp_path / "g.part").exists()

    def test_a_short_body_leaves_a_truncated_dest_when_not_atomic(self, tmp_path):
        """Without staging there is no partial to discard, only the caller's own file."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], advertised=22))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(IncompleteDownloadError):
            client.download("http://x/g", dest, progress=False, atomic=False)
        assert dest.read_bytes() == _PAYLOAD[:8]

    def test_a_body_without_a_length_is_published_unchecked(self, tmp_path):
        """A chunked response makes no length claim, so `download` makes none either."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, omit_length=True))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g", dest, progress=False
        )
        assert dest.read_bytes() == _PAYLOAD

    def test_a_failed_magic_check_publishes_nothing(self, tmp_path):
        """The magic check runs before the rename, not after."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        client = HttpClient(session=session, sleep=lambda _: None)
        with pytest.raises(ValueError):
            client.download("http://x/g", dest, progress=False, expect_magic=b"XYZ")
        assert not dest.exists()
        assert not (tmp_path / "g.part").exists()


@pytest.mark.unit
class TestUnsolicitedPartialContent:
    """A `206` answering a Range-less request is refused, for every client shape."""

    @pytest.mark.parametrize(
        "retry_on_exceptions",
        [
            None,
            (requests.RequestException, OSError),
            (requests.RequestException,),
            (requests.ConnectionError, requests.Timeout, OSError),
        ],
        ids=[
            "default",
            "requestexception-oserror",
            "requestexception",
            "conn-timeout-os",
        ],
    )
    def test_it_is_refused_in_exactly_one_request(self, tmp_path, retry_on_exceptions):
        """Replay returns the same fragment, so no retry policy may retry it."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], status=206))
        extra = (
            {}
            if retry_on_exceptions is None
            else {"retry_on_exceptions": retry_on_exceptions}
        )
        client = HttpClient(
            session=session, sleep=lambda _: None, max_retries=5, **extra
        )
        with pytest.raises(UnsolicitedPartialContentError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 1, f"expected one request, got {session.calls}"
        assert not (tmp_path / "g.part").exists()

    def test_a_206_answering_a_caller_range_is_accepted(self, tmp_path):
        """A caller who asked for a range gets the fragment they requested."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], status=206))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g", dest, progress=False, headers={"Range": "bytes=0-7"}
        )
        assert dest.read_bytes() == _PAYLOAD[:8]

    def test_a_caller_range_is_recognised_in_any_casing(self, tmp_path):
        """The Range test folds case, so a lowercase header still counts as solicited."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], status=206))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/g", dest, progress=False, headers={"range": "bytes=0-7"}
        )
        assert dest.read_bytes() == _PAYLOAD[:8]


@pytest.mark.unit
class TestDownloadRetryBudgets:
    """How many requests each failure kind is worth."""

    def test_a_short_body_is_retried_then_succeeds(self, tmp_path):
        """A transient truncation is worth one more read."""
        dest = tmp_path / "g"
        session = _ScriptedSession(
            _ScriptedBody(_PAYLOAD[:8], advertised=22), _ScriptedBody(_PAYLOAD)
        )
        HttpClient(session=session, sleep=lambda _: None, max_retries=3).download(
            "http://x/g", dest, progress=False
        )
        assert dest.read_bytes() == _PAYLOAD
        assert session.calls == 2

    def test_an_identical_short_body_is_not_retried_to_exhaustion(self, tmp_path):
        """The same byte count twice is deterministic, not a blip."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], advertised=22))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(IncompleteDownloadError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 2, f"expected 2 attempts, got {session.calls}"

    def test_an_over_long_body_is_not_retried(self, tmp_path):
        """A stream that disagrees with its own header repeats that disagreement."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, advertised=8))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(IncompleteDownloadError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 1, f"expected 1 attempt, got {session.calls}"

    def test_opting_out_of_transport_retry_disables_the_length_retry(self, tmp_path):
        """`retry_on_exceptions=()` is the documented opt-out and is honoured here too."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], advertised=22))
        client = HttpClient(
            session=session, sleep=lambda _: None, max_retries=5, retry_on_exceptions=()
        )
        with pytest.raises(IncompleteDownloadError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 1

    def test_zero_max_retries_makes_one_attempt(self, tmp_path):
        """Nine backends opt out this way, and the per-kind budgets must not re-arm them."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(requests.RequestException):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        assert session.calls == 1

    def test_a_connect_failure_is_retried_then_succeeds(self, tmp_path):
        """The canary for `_send` living inside the retrying `try`."""
        dest = tmp_path / "g"
        session = _ConnectThenGood()
        HttpClient(session=session, sleep=lambda _: None, max_retries=3).download(
            "http://x/g", dest, progress=False
        )
        assert dest.read_bytes() == _PAYLOAD
        assert session.calls == 2, f"expected 2 requests, got {session.calls}"

    def test_a_connect_failure_spends_only_the_connect_budget(self, tmp_path):
        """A dead host must not cost a large download the full read budget."""
        session = _AlwaysRaises(_connect_refused())
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(requests.ConnectionError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == DEFAULT_CONNECT_RETRIES + 1

    def test_a_read_failure_spends_the_read_budget(self, tmp_path):
        """A mid-response reset is the genuinely transient case, so it keeps the budget."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=3)
        with pytest.raises(requests.ConnectionError):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        assert session.calls == 4

    def test_a_deterministic_transport_error_is_not_retried(self, tmp_path):
        """An invalid certificate reproduces exactly, so replaying it only delays it."""
        session = _AlwaysRaises(requests.exceptions.SSLError("bad cert"))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=5)
        with pytest.raises(requests.exceptions.SSLError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 1

    def test_a_416_is_only_an_error_status_now(self, tmp_path):
        """Nothing asks for a range, so a 416 carries no special meaning."""
        session = _ScriptedSession(_ScriptedBody(b"", status=416))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(requests.HTTPError):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert not (tmp_path / "g.part").exists()


@pytest.mark.unit
class TestNoExitPathLeavesAPartial:
    """A `.part` from an earlier call must not survive this call, however it ends."""

    @pytest.mark.parametrize(
        "session, expected",
        [
            (
                _ScriptedSession(_ScriptedBody(_PAYLOAD[:8], status=206)),
                UnsolicitedPartialContentError,
            ),
            (_ScriptedSession(_ScriptedBody(b"", status=416)), requests.HTTPError),
            (
                _ScriptedSession(_ScriptedBody(_PAYLOAD, advertised=8)),
                IncompleteDownloadError,
            ),
            (
                _AlwaysRaises(requests.exceptions.SSLError("bad cert")),
                requests.exceptions.SSLError,
            ),
            (_AlwaysRaises(_connect_refused()), requests.ConnectionError),
            (
                _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5)),
                requests.ConnectionError,
            ),
        ],
        ids=["unsolicited-206", "416", "over-long", "bad-cert", "refused", "reset"],
    )
    def test_a_stale_partial_is_discarded_however_the_attempt_fails(
        self, tmp_path, session, expected
    ):
        """Each refusal path calls the same cleanup, so none may skip it."""
        dest = tmp_path / "g"
        (tmp_path / "g.part").write_bytes(b"Z" * 999)
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(expected):
            client.download("http://x/g", dest, progress=False, chunk=1)
        assert not (tmp_path / "g.part").exists()
        assert not dest.exists()


@pytest.mark.unit
class TestDownloadSalvage:
    """A break after the last byte does not cost another read."""

    def test_a_complete_body_that_breaks_late_is_kept(self, tmp_path):
        """The size equality is the whole proof, so no second request is made."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, break_at_end=True))
        HttpClient(session=session, sleep=lambda _: None, max_retries=3).download(
            "http://x/g", dest, progress=False, chunk=1
        )
        assert dest.read_bytes() == _PAYLOAD
        assert session.calls == 1, f"expected 1 request, got {session.calls}"

    def test_a_late_break_is_salvaged_even_with_retry_disabled(self, tmp_path):
        """The salvage issues no request, so the retry policy is not its gate."""
        dest = tmp_path / "g"
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, break_at_end=True))
        HttpClient(
            session=session,
            sleep=lambda _: None,
            max_retries=0,
            retry_on_exceptions=(),
        ).download("http://x/g", dest, progress=False, chunk=1)
        assert dest.read_bytes() == _PAYLOAD
        assert session.calls == 1

    def test_a_late_break_without_a_length_is_not_salvaged(self, tmp_path):
        """With no advertised total there is nothing to compare against."""
        session = _ScriptedSession(
            _ScriptedBody(_PAYLOAD, break_at_end=True, omit_length=True)
        )
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(requests.ConnectionError):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        assert not (tmp_path / "g.part").exists()

    def test_a_salvaged_body_still_has_to_pass_the_magic_check(self, tmp_path):
        """A break after the last byte must not be a weaker door to `dest`.

        An error page served with a 200 whose `Content-Length` happens to match
        is exactly what `expect_magic` exists to stop, and it reaches the
        salvage branch whenever the stream breaks as it ends.
        """
        dest = tmp_path / "grid.nc"
        page = b"<html>an error page served with a 200</html>"
        session = _ScriptedSession(_ScriptedBody(page, break_at_end=True))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=2)
        with pytest.raises(ValueError):
            client.download(
                "http://x/grid.nc", dest, progress=False, chunk=8, expect_magic=b"CDF"
            )
        assert not dest.exists(), "an unverified body was published"
        assert not (tmp_path / "grid.nc.part").exists()

    def test_a_salvaged_body_with_the_right_magic_is_published(self, tmp_path):
        """The gate must not break the salvage it guards."""
        dest = tmp_path / "grid.nc"
        body = b"CDF" + b"payload" * 10
        session = _ScriptedSession(_ScriptedBody(body, break_at_end=True))
        HttpClient(session=session, sleep=lambda _: None).download(
            "http://x/grid.nc", dest, progress=False, chunk=8, expect_magic=b"CDF"
        )
        assert dest.read_bytes() == body
        assert session.calls == 1

    def test_an_incomplete_body_that_breaks_is_not_salvaged(self, tmp_path):
        """Without the equality there is no proof, so the error propagates."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5))
        client = HttpClient(session=session, sleep=lambda _: None, max_retries=0)
        with pytest.raises(requests.ConnectionError):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        assert not (tmp_path / "g.part").exists()


@pytest.mark.unit
class TestDownloadCleanupAndOrdering:
    """Nothing is left behind, and nothing is held open across a sleep."""

    def test_an_interrupt_from_the_backoff_sleep_leaves_nothing_behind(self, tmp_path):
        """A `BaseException` from the sleep still unwinds the staging file."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD, stop_after=5))
        client = HttpClient(session=session, sleep=_raise_interrupt, max_retries=3)
        with pytest.raises(KeyboardInterrupt):
            client.download("http://x/g", tmp_path / "g", progress=False, chunk=1)
        assert not (tmp_path / "g.part").exists()
        assert not (tmp_path / "g").exists()

    def test_an_interrupt_from_the_throttle_leaves_nothing_behind(self, tmp_path):
        """The throttle sleeps before the request, and must publish nothing either."""
        session = _ScriptedSession(_ScriptedBody(_PAYLOAD))
        client = HttpClient(
            session=session,
            sleep=_raise_interrupt,
            min_interval=5.0,
            clock=lambda: 0.0,
        )
        client._last_request = 0.0
        with pytest.raises(KeyboardInterrupt):
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert session.calls == 0
        assert not (tmp_path / "g.part").exists()
        assert not (tmp_path / "g").exists()

    def test_the_response_is_closed_before_the_backoff_sleep(self, tmp_path):
        """A streamed socket must not be held open across a retry wait."""
        events: list[str] = []
        session = _ClosingSession(events)
        client = HttpClient(
            session=session, sleep=lambda wait: events.append("sleep"), max_retries=3
        )
        client.download("http://x/g", tmp_path / "g", progress=False)
        assert events[:2] == ["close", "sleep"], f"ordering was {events}"


@pytest.mark.unit
class TestDownloadProgressBar:
    """The bar describes one attempt, never an offset into a previous one."""

    def test_one_bar_per_attempt_starting_from_zero(self, tmp_path, monkeypatch):
        """`initial=` was only ever meaningful for a resume, which no longer exists."""
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr("earthlens.base.http.tqdm", _RecordingTqdm(seen))
        session = _ScriptedSession(
            _ScriptedBody(_PAYLOAD[:8], advertised=22), _ScriptedBody(_PAYLOAD)
        )
        HttpClient(session=session, sleep=lambda _: None, max_retries=3).download(
            "http://x/g", tmp_path / "g", progress=False
        )
        assert len(seen) == 2, f"expected one bar per attempt, got {len(seen)}"
        assert all("initial" not in kwargs for kwargs in seen), seen
        assert [kwargs["total"] for kwargs in seen] == [22, 22]


class _WriteFails:
    """Serves a good response whose body raises `OSError` on the first block."""

    def __init__(self, exc: OSError) -> None:
        self._exc = exc
        self.calls = 0

    def get(self, url: str, **kwargs: Any) -> _ScriptedBody:
        """Return a body that will fail while being written."""
        self.calls += 1
        return _RaisingBody(self._exc)


class _RaisingBody(_ScriptedBody):
    """A body whose stream raises the injected error instead of yielding."""

    def __init__(self, exc: OSError) -> None:
        super().__init__(_PAYLOAD)
        self._exc = exc

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Raise instead of yielding, standing in for a failing write."""
        raise self._exc
        yield b""  # pragma: no cover - makes this a generator


@pytest.mark.unit
class TestLocalStorageFailuresAreDeterministic:
    """A failure of the destination is not a transport blip."""

    @pytest.mark.parametrize(
        "name", ["ENOSPC", "EROFS", "EACCES", "EPERM", "EFBIG", "EISDIR"]
    )
    def test_a_filesystem_refusal_is_never_retryable(self, name):
        """No retry makes the disk larger or the mount writable."""
        code = getattr(errno, name)
        exc = OSError(code, "refused")
        assert classify_transport_error(exc, strict=True) is None
        assert classify_transport_error(exc, strict=False) is None

    def test_a_wrapped_socket_error_is_still_a_transport_failure(self):
        """`ConnectionResetError` is an `OSError` too, and must stay retryable."""
        wrapped = requests.ConnectionError(ConnectionResetError("reset"))
        assert classify_transport_error(wrapped) == "read"

    def test_a_bare_socket_error_still_honours_a_caller_that_named_oserror(self):
        """The disk rule must not swallow the socket errors that share the type."""
        bare = ConnectionResetError(errno.ECONNRESET, "reset")
        assert classify_transport_error(bare, strict=False) == "read"

    def test_an_unrecognised_errno_keeps_the_callers_choice(self):
        """The rule names specific conditions rather than claiming every `OSError`."""
        exc = OSError(errno.EINTR, "interrupted")
        assert classify_transport_error(exc, strict=True) is None
        assert classify_transport_error(exc, strict=False) == "read"

    def test_a_requests_error_is_not_mistaken_for_a_disk_error(self):
        """`RequestException` subclasses `OSError`, so the check must exclude it."""
        assert not _is_local_storage_error(requests.ConnectionError("boom"))
        assert not _is_local_storage_error(
            IncompleteDownloadError("short", written=1, expected=2)
        )

    def test_a_full_disk_is_not_retried_by_a_caller_that_named_oserror(self, tmp_path):
        """The three archive backends pass `OSError`, and would otherwise refetch."""
        session = _WriteFails(OSError(errno.ENOSPC, "No space left on device"))
        client = HttpClient(
            session=session,
            sleep=lambda _: None,
            max_retries=5,
            retry_on_exceptions=(requests.RequestException, OSError),
        )
        with pytest.raises(OSError) as excinfo:
            client.download("http://x/g", tmp_path / "g", progress=False)
        assert excinfo.value.errno == errno.ENOSPC
        assert session.calls == 1, f"expected 1 attempt, got {session.calls}"
        assert not (tmp_path / "g.part").exists()


# ---------------------------------------------------------------------------
# Opt-in resume.
#
# `_ResumeServer` below is a range-serving object store whose misbehaviour is
# configurable, so each adversarial shape the design was attacked with is a
# constructor argument rather than a bespoke fake.
# ---------------------------------------------------------------------------

_OBJECT = bytes(range(256)) * 800  # 204,800 bytes, > _RESUME_OVERLAP
_BREAK_AT = 120_000  # leaves a staged prefix comfortably above the overlap


class _ResumeBody:
    """One response from `_ResumeServer`."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int,
        headers: dict[str, str],
        break_at: int | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status
        self.headers = headers
        self.break_at = break_at
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Yield the payload, breaking at `break_at` if one was set."""
        emitted = 0
        for i in range(0, len(self.payload), chunk_size):
            block = self.payload[i : i + chunk_size]
            if self.break_at is not None and emitted + len(block) > self.break_at:
                give = self.break_at - emitted
                if give > 0:
                    yield block[:give]
                raise _read_reset()
            emitted += len(block)
            yield block

    def raise_for_status(self) -> None:
        """Raise for a 4xx/5xx, as `requests` does."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        """Record the release."""
        self.closed = True


class _ResumeServer:
    """A range-serving store whose every misbehaviour is a constructor flag."""

    def __init__(
        self,
        payload: bytes = _OBJECT,
        *,
        etag: str | None = '"v1"',
        accept_ranges: bool = True,
        encoding: str | None = None,
        break_first_at: int | None = _BREAK_AT,
        break_every_whole: bool = False,
        honour_range: bool = True,
        leg_status: int | None = None,
        leg_etag: str | None = None,
        leg_encoding: str | None = None,
        leg_content_range: str | None = None,
        leg_payload: bytes | None = None,
        leg_break_at: int | None = None,
        leg_content_length: str | None = None,
        leg_transfer_encoding: str | None = None,
    ) -> None:
        self.payload = payload
        self.etag = etag
        self.accept_ranges = accept_ranges
        self.encoding = encoding
        self.break_first_at = break_first_at
        self.break_every_whole = break_every_whole
        self.honour_range = honour_range
        self.leg_status = leg_status
        self.leg_etag = leg_etag
        self.leg_encoding = leg_encoding
        self.leg_content_range = leg_content_range
        self.leg_payload = leg_payload
        self.leg_break_at = leg_break_at
        self.leg_content_length = leg_content_length
        self.leg_transfer_encoding = leg_transfer_encoding
        self.requests: list[dict[str, str]] = []

    @property
    def calls(self) -> int:
        """How many requests were issued."""
        return len(self.requests)

    @property
    def ranged(self) -> list[dict[str, str]]:
        """Just the requests that carried a `Range`."""
        return [r for r in self.requests if "Range" in r]

    def get(self, url: str, **kwargs: Any) -> _ResumeBody:
        """Answer a whole-object or ranged request."""
        sent = dict(kwargs.get("headers") or {})
        self.requests.append(sent)
        wanted = sent.get("Range")
        if wanted is None or not self.honour_range:
            return self._whole()
        first, last = (int(p) for p in wanted.removeprefix("bytes=").split("-"))
        return self._partial(first, last)

    def _base_headers(self) -> dict[str, str]:
        """Headers common to both response shapes."""
        headers: dict[str, str] = {}
        if self.etag is not None:
            headers["ETag"] = self.etag
        if self.accept_ranges:
            headers["Accept-Ranges"] = "bytes"
        if self.encoding:
            headers["Content-Encoding"] = self.encoding
        return headers

    def _whole(self) -> _ResumeBody:
        """The 200 answer, optionally breaking part-way."""
        headers = self._base_headers()
        headers["Content-Length"] = str(len(self.payload))
        breaks = self.break_every_whole or len(self.requests) == 1
        return _ResumeBody(
            self.payload,
            status=200,
            headers=headers,
            break_at=self.break_first_at if breaks else None,
        )

    def _partial(self, first: int, last: int) -> _ResumeBody:
        """The 206 answer, with whatever misbehaviour was configured."""
        headers = self._base_headers()
        if self.leg_etag is not None:
            headers["ETag"] = self.leg_etag
        if self.leg_encoding:
            headers["Content-Encoding"] = self.leg_encoding
        body = (
            self.leg_payload
            if self.leg_payload is not None
            else self.payload[first : last + 1]
        )
        headers["Content-Range"] = (
            self.leg_content_range
            if self.leg_content_range is not None
            else f"bytes {first}-{last}/{len(self.payload)}"
        )
        # A self-consistent lie: derive Content-Length from the range the
        # response *claims*, so the length cross-check cannot stand in for the
        # start/end/total gates under test.
        claimed = _parse_content_range(headers["Content-Range"])
        headers["Content-Length"] = self.leg_content_length or str(
            claimed[1] - claimed[0] + 1 if claimed else len(body)
        )
        if self.leg_transfer_encoding:
            headers["Transfer-Encoding"] = self.leg_transfer_encoding
        return _ResumeBody(
            body,
            status=self.leg_status or 206,
            headers=headers,
            break_at=self.leg_break_at,
        )


def _resume_client(server: _ResumeServer, **kwargs: Any) -> HttpClient:
    """Build a client over a resume server with no real sleeping."""
    kwargs.setdefault("max_retries", 3)
    return HttpClient(session=server, sleep=lambda _: None, **kwargs)


@pytest.mark.unit
class TestResumeIsOptIn:
    """Nothing about the default path changes."""

    def test_no_range_is_sent_without_the_flag(self, tmp_path):
        """The 1400 existing call sites must behave exactly as before."""
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", tmp_path / "g", progress=False, chunk=4096
        )
        assert server.ranged == []

    def test_the_object_still_arrives_intact_without_the_flag(self, tmp_path):
        """A mid-stream break is still repaired by a full restart."""
        dest = tmp_path / "g"
        server = _ResumeServer()
        _resume_client(server).download("http://x/g", dest, progress=False, chunk=4096)
        assert dest.read_bytes() == _OBJECT


@pytest.mark.unit
class TestResumeHappyPath:
    """What the feature is for."""

    def test_a_broken_transfer_is_resumed_rather_than_restarted(self, tmp_path):
        """The second request asks for the tail, and the file is byte-correct."""
        dest = tmp_path / "g"
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert server.calls == 2
        sent = server.ranged[0]
        assert sent["Range"] == f"bytes={_BREAK_AT - 65536}-{len(_OBJECT) - 1}"
        assert sent["If-Range"] == '"v1"'
        assert sent["Accept-Encoding"] == "identity"

    def test_the_resumed_leg_re_fetches_the_overlap(self, tmp_path):
        """The window starts before the staged end so the bytes can be compared."""
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", tmp_path / "g", progress=False, chunk=4096, resume=True
        )
        first = int(server.ranged[0]["Range"].removeprefix("bytes=").split("-")[0])
        assert _BREAK_AT - first == 65536

    def test_no_part_file_survives_a_resumed_download(self, tmp_path):
        """The staging file is still renamed, not left behind."""
        _resume_client(_ResumeServer()).download(
            "http://x/g", tmp_path / "g", progress=False, chunk=4096, resume=True
        )
        assert not (tmp_path / "g.part").exists()


@pytest.mark.unit
class TestResumeRefusesBadResponses:
    """Every gate, one server misbehaviour each."""

    @pytest.mark.parametrize(
        "kwargs, reason",
        [
            ({"honour_range": False}, "answers 200 with the whole object"),
            ({"leg_status": 416}, "answers 416"),
            ({"leg_content_range": "bytes 999-204799/204800"}, "wrong range start"),
            (
                {"leg_content_range": f"bytes {_BREAK_AT - 65536}-204798/204800"},
                "range stops short of the object end",
            ),
            (
                {"leg_content_range": f"bytes {_BREAK_AT - 65536}-204799/999999"},
                "range reports a different complete length",
            ),
            ({"leg_content_range": "bytes */204800"}, "unparseable Content-Range"),
            ({"leg_content_range": None, "leg_etag": '"v2"'}, "renamed ETag"),
            ({"leg_encoding": "gzip"}, "coded ranged reply"),
            ({"leg_transfer_encoding": "chunked"}, "chunked ranged reply"),
            (
                {"leg_content_length": "12"},
                "Content-Length disagrees with the Content-Range",
            ),
        ],
        ids=[
            "200-whole",
            "416",
            "wrong-start",
            "wrong-end",
            "wrong-total",
            "star-range",
            "etag-change",
            "gzip-leg",
            "chunked-leg",
            "length-mismatch",
        ],
    )
    def test_a_bad_leg_falls_back_to_a_whole_read(self, tmp_path, kwargs, reason):
        """The leg must be refused, not merely survived.

        Asserting only that the file is right would pass with the gate deleted,
        because this fake is honest about the bytes it sends. The request count
        is what distinguishes "refused and re-read the object" (three requests)
        from "accepted the bad leg" (two).
        """
        dest = tmp_path / "g"
        server = _ResumeServer(**kwargs)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert len(server.ranged) == 1, reason
        assert server.calls == 3, f"the leg was accepted despite {reason}"
        assert dest.read_bytes() == _OBJECT
        assert not (tmp_path / "g.part").exists()

    def test_a_lying_body_with_truthful_headers_is_caught(self, tmp_path):
        """The attack no header check can see: right Content-Range, wrong bytes.

        Every arithmetic gate passes here, so only the overlap comparison
        stands between this and a published file that never existed upstream.
        """
        dest = tmp_path / "g"
        start = _BREAK_AT - 65536
        wrong = bytes(1 for _ in range(len(_OBJECT) - start))
        server = _ResumeServer(leg_payload=wrong)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert b"\x01" * 200 not in dest.read_bytes()

    def test_a_leg_ending_inside_the_overlap_is_refused(self, tmp_path):
        """There are not enough bytes to compare, so nothing may be appended."""
        dest = tmp_path / "g"
        server = _ResumeServer(leg_payload=b"z" * 100)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert server.calls == 3

    def test_a_chunk_size_that_straddles_the_overlap_still_resumes(self, tmp_path):
        """The overlap rarely lands on a chunk boundary, so the split is exercised."""
        dest = tmp_path / "g"
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=5000, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert server.calls == 2

    def test_a_refusal_disarms_resume_for_the_rest_of_the_call(self, tmp_path):
        """One strike, so a bad server cannot drive a restart/resume cycle.

        Every whole-object read breaks here, so without the disarm the call
        would bank and re-arm on each pass and issue a ranged request per retry.
        """
        server = _ResumeServer(honour_range=False, break_every_whole=True)
        client = _resume_client(server, max_retries=4)
        with pytest.raises(requests.RequestException):
            client.download(
                "http://x/g", tmp_path / "g", progress=False, chunk=4096, resume=True
            )
        assert len(server.ranged) == 1, (
            f"resume re-armed after a refusal: {len(server.ranged)} ranged requests"
        )


@pytest.mark.unit
class TestResumeArmingConditions:
    """When a ranged request is never worth attempting."""

    @pytest.mark.parametrize(
        "kwargs, why",
        [
            ({"etag": 'W/"7"'}, "a weak validator cannot anchor a representation"),
            ({"etag": None}, "no validator at all"),
            ({"accept_ranges": False}, "the server does not advertise ranges"),
            ({"encoding": "gzip"}, "a coded body makes the offset meaningless"),
        ],
        ids=["weak-etag", "no-etag", "no-accept-ranges", "coded-body"],
    )
    def test_it_never_arms(self, tmp_path, kwargs, why):
        """No Range is sent at all, and the object still arrives."""
        dest = tmp_path / "g"
        server = _ResumeServer(**kwargs)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert server.ranged == [], why
        assert dest.read_bytes() == _OBJECT

    def test_a_callers_own_range_disables_resume(self, tmp_path):
        """If the caller owns the object boundaries, we never add ours on top."""
        server = _ResumeServer(break_first_at=None)
        _resume_client(server).download(
            "http://x/g",
            tmp_path / "g",
            progress=False,
            chunk=4096,
            resume=True,
            headers={"Range": "bytes=0-99"},
        )
        assert all(r["Range"] == "bytes=0-99" for r in server.ranged)

    def test_a_break_inside_the_overlap_does_not_arm(self, tmp_path):
        """Below the overlap a resumed leg would make no forward progress."""
        dest = tmp_path / "g"
        server = _ResumeServer(break_first_at=1024)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert server.ranged == []
        assert dest.read_bytes() == _OBJECT


@pytest.mark.unit
class TestResumeBanksOnlyWhatIsOnDisk:
    """The offset a resumed request asks for must equal the staged file size."""

    def test_an_earlier_larger_attempt_does_not_inflate_the_offset(self, tmp_path):
        """A high-water mark across attempts outlives the file that produced it.

        Attempt 1 ends cleanly but short, so it is discarded; attempt 2 breaks
        earlier. Banking the larger, older count would aim the ranged request
        past the end of the staged file, guaranteeing a refusal and blaming the
        server for a local accounting error.
        """
        dest = tmp_path / "g"
        server = _ShrinkingResumeServer()
        _resume_client(server, max_retries=5).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        ranged = server.ranged
        assert ranged, "the second break should still have armed a resume"
        first = int(ranged[0]["Range"].removeprefix("bytes=").split("-")[0])
        assert first == 100_000 - 65536, (
            f"the leg asked from {first}, so it banked something other than the "
            f"100,000 bytes attempt 2 actually wrote"
        )


class _ShrinkingResumeServer(_ResumeServer):
    """Ends short on the first read, then breaks earlier on the second."""

    def _whole(self) -> _ResumeBody:
        """Serve a clean-but-short body, then a body that breaks at 100,000."""
        headers = self._base_headers()
        headers["Content-Length"] = str(len(self.payload))
        n = len(self.requests)
        if n == 1:
            # Ends cleanly at 190,000: an IncompleteDownloadError, discarded.
            return _ResumeBody(self.payload[:190_000], status=200, headers=headers)
        return _ResumeBody(self.payload, status=200, headers=headers, break_at=100_000)


@pytest.mark.unit
class TestResumeHelperEdges:
    """Branches of the resume helpers a server-level test cannot reach."""

    @pytest.mark.parametrize(
        "value", ["abc", '"unterminated', 'trailing"', '"', ""], ids=list("abcde")
    )
    def test_a_malformed_entity_tag_does_not_anchor(self, value):
        """An `ETag` that is not a quoted-string cannot identify a representation."""
        assert _strong_etag({"ETag": value}) is None

    def test_empty_keepalive_blocks_do_not_count_toward_the_overlap(self, tmp_path):
        """A chunked keepalive yields empty blocks; they carry no bytes to compare."""
        dest = tmp_path / "g"
        server = _KeepaliveResumeServer()
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert server.calls == 2

    def test_an_unreadable_staging_file_disarms_instead_of_raising(
        self, tmp_path, monkeypatch
    ):
        """A local read-back failure is not the server's fault, so restart quietly."""
        dest = tmp_path / "g"
        server = _ResumeServer()
        real_open = builtins.open

        def _failing_open(file, mode="r", *args, **kwargs):
            if str(file).endswith(".part") and "r" in mode and "+" not in mode:
                raise OSError(errno.ENOENT, "vanished")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _failing_open)
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert server.calls == 3, "the leg should have been refused, not raised"


class _KeepaliveResumeServer(_ResumeServer):
    """A resume server whose ranged body is padded with empty blocks."""

    def _partial(self, first: int, last: int) -> _ResumeBody:
        """Return the normal 206, but yielding empty blocks between real ones."""
        body = super()._partial(first, last)
        return _EmptyPaddedBody(body)


class _EmptyPaddedBody:
    """Wraps a body, interleaving zero-length blocks into its stream."""

    def __init__(self, inner: _ResumeBody) -> None:
        self._inner = inner
        self.status_code = inner.status_code
        self.headers = inner.headers

    def iter_content(self, chunk_size: int = 1) -> Any:
        """Yield an empty block before every real one."""
        for block in self._inner.iter_content(chunk_size):
            yield b""
            yield block

    def raise_for_status(self) -> None:
        """Delegate."""
        self._inner.raise_for_status()

    def close(self) -> None:
        """Delegate."""
        self._inner.close()


@pytest.mark.unit
class TestResumeNeverTrustsFoundBytes:
    """A `.part` this call did not write is never resumed from."""

    def test_a_stale_part_is_not_used_as_the_resume_prefix(self, tmp_path):
        """Otherwise the published file is half a previous, unrelated object."""
        dest = tmp_path / "g"
        (tmp_path / "g.part").write_bytes(b"\xff" * 190_000)
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", dest, progress=False, chunk=4096, resume=True
        )
        assert dest.read_bytes() == _OBJECT
        assert b"\xff" * 1000 not in dest.read_bytes()

    def test_the_resumed_leg_starts_from_this_calls_own_byte_count(self, tmp_path):
        """The offset must come from the truncating write, not the stale size."""
        (tmp_path / "g.part").write_bytes(b"\xff" * 190_000)
        server = _ResumeServer()
        _resume_client(server).download(
            "http://x/g", tmp_path / "g", progress=False, chunk=4096, resume=True
        )
        first = int(server.ranged[0]["Range"].removeprefix("bytes=").split("-")[0])
        assert first == _BREAK_AT - 65536, "offset was taken from the stale file"


@pytest.mark.unit
class TestResumeStillVerifies:
    """A resumed file is published through the same gates as a whole read."""

    def test_a_resumed_leg_that_ends_short_does_not_publish(self, tmp_path):
        """The length post-condition applies to the assembled file too."""
        dest = tmp_path / "g"
        server = _ResumeServer(leg_break_at=1000)
        client = _resume_client(server, max_retries=1)
        with pytest.raises(requests.RequestException):
            client.download("http://x/g", dest, progress=False, chunk=4096, resume=True)
        assert not dest.exists()
        assert not (tmp_path / "g.part").exists()

    def test_a_resumed_file_is_magic_checked(self, tmp_path):
        """`expect_magic` runs on the assembled object, not on the first leg."""
        dest = tmp_path / "g"
        server = _ResumeServer()
        client = _resume_client(server)
        with pytest.raises(ValueError):
            client.download(
                "http://x/g",
                dest,
                progress=False,
                chunk=4096,
                resume=True,
                expect_magic=b"NOPE",
            )
        assert not dest.exists()

    def test_a_refusal_with_no_budget_left_raises_instead_of_looping(self, tmp_path):
        """The refusal path is bounded by the retry budget, not only by the disarm."""
        server = _ResumeServer(honour_range=False, break_every_whole=True)
        client = _resume_client(server, max_retries=1)
        with pytest.raises(requests.ConnectionError, match="no retry budget left"):
            client.download(
                "http://x/g", tmp_path / "g", progress=False, chunk=4096, resume=True
            )
        assert not (tmp_path / "g.part").exists()

    def test_a_resumed_download_matches_a_restarted_one_byte_for_byte(self, tmp_path):
        """The whole point: resuming must not change what lands on disk."""
        restarted = tmp_path / "restarted"
        resumed = tmp_path / "resumed"
        _resume_client(_ResumeServer()).download(
            "http://x/g", restarted, progress=False, chunk=4096
        )
        _resume_client(_ResumeServer()).download(
            "http://x/g", resumed, progress=False, chunk=4096, resume=True
        )
        assert restarted.read_bytes() == resumed.read_bytes() == _OBJECT
