"""Shared `requests`-based HTTP client for the REST-style backends.

Every REST / event / air-quality / biodiversity backend repeats the
same transport chores: a pooled `requests.Session`, a sensible default
`User-Agent` and `Accept-Encoding`, a per-request timeout, a
`Retry-After`-aware retry/back-off loop for `429`/`5xx`, JSON decoding,
and a streamed `download()` with a `tqdm` progress bar. `HttpClient`
owns exactly those transport concerns so a backend keeps only the
`API`-shaped parts: endpoint paths, query params, pagination envelopes,
auth-header values, and response parsing.

The retry engine is a hand-rolled loop (not `urllib3.util.retry.Retry`
mounted on an `HTTPAdapter`) generalising the loop the REST backends
(openaq first) previously hand-rolled: `Retry-After` first, else
`backoff_factor * 2**attempt`. Both the transport
(`session=`) and the wait (`sleep=`) are injectable, so the whole client
is unit-testable with a fake transport — no live network, no real
delays.

`requests` and `tqdm` are already core earthlens dependencies, so this
module adds none. The public import is
`from earthlens.base.http import HttpClient`.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from loguru import logger
from tqdm import tqdm

#: Per-request timeout (seconds) applied when a call passes no `timeout`.
DEFAULT_TIMEOUT = 60.0

#: Maximum retries for a retryable status before the last response's
#: error is raised.
DEFAULT_MAX_RETRIES = 5

#: Base seconds for exponential back-off when a response carries no
#: `Retry-After` header (wait = `backoff_factor * 2**attempt`).
DEFAULT_BACKOFF_FACTOR = 1.0

#: Ceiling (seconds) on any single retry wait, so a hostile or
#: misconfigured `Retry-After` cannot pin the calling thread for an
#: unbounded interval. `None` disables the cap.
DEFAULT_MAX_BACKOFF = 300.0

#: HTTP statuses that trigger a retry. `429` (rate-limited) plus the
#: transient `5xx` gateway/unavailable family.
DEFAULT_STATUS_FORCELIST: tuple[int, ...] = (429, 500, 502, 503, 504)

#: Streaming chunk size (bytes) for :meth:`HttpClient.download` — 1 MiB.
DEFAULT_CHUNK_SIZE = 1 << 20


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    RFC 9110 §10.2.3 allows either an integer number of seconds (e.g.
    `Retry-After: 5`) or an HTTP-date
    (`Retry-After: Fri, 31 Dec 2027 23:59:59 GMT`); both forms are
    handled. A missing, non-numeric, or **negative** numeric value yields
    `None` so the caller falls back to exponential back-off (the spec
    mandates a non-negative delay; a negative one is invalid). A past
    HTTP-date clamps to `0.0` (never sleep backwards in time).

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable /
        negative.

    Examples:
        - A numeric value parses to seconds; junk or a negative yields
          `None`, and a past HTTP-date clamps to zero:
            ```python
            >>> from earthlens.base.http import _parse_retry_after
            >>> _parse_retry_after("5")
            5.0
            >>> _parse_retry_after(None) is None
            True
            >>> _parse_retry_after("soon") is None
            True
            >>> _parse_retry_after("-1") is None
            True
            >>> _parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT")
            0.0

            ```
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = dt.datetime.now(tz=target.tzinfo)
    return max(0.0, (target - now).total_seconds())


def _redact_url(url: str) -> str:
    """Return `scheme://host` for logging, hiding any secret in the URL.

    Retry warnings must never echo the full request URL: some backends
    carry a credential in the URL itself — a path segment (FIRMS
    `MAP_KEY`), a query parameter (NREL `api_key`, WDPA `?token=`), or
    userinfo (`user:pass@host`). Everything but the (non-secret) scheme
    and host is dropped, so only `scheme://host` reaches the log.

    Args:
        url: The request URL.

    Returns:
        `"scheme://host"`, or `"<url>"` when the input has no scheme/host.

    Examples:
        - The path, query, and userinfo — where secrets ride — are
          stripped:
            ```python
            >>> from earthlens.base.http import _redact_url
            >>> _redact_url("https://firms.example/api/SECRETKEY/area?x=1")
            'https://firms.example'
            >>> _redact_url("https://user:pass@host/p")
            'https://host'

            ```
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return "<url>"
    return f"{parts.scheme}://{parts.hostname}"


def _progress_total(headers: Any) -> int | None:
    """Return the download progress-bar total from response headers.

    Uses `Content-Length` only for an untransformed body: it reports the
    *compressed* size, but `iter_content` yields *decompressed* bytes, so
    a `Content-Encoding` (e.g. gzip) response would overshoot the bar. In
    that case (or when the length is absent / non-numeric) the total is
    `None` and the bar runs unbounded.

    Args:
        headers: The response headers mapping.

    Returns:
        The byte total, or `None` when it cannot be trusted.
    """
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        return None
    raw_length = headers.get("Content-Length")
    if raw_length is not None and raw_length.isdigit():
        return int(raw_length)
    return None


def _default_user_agent() -> str:
    """Return the default `User-Agent`, `earthlens/{version}`.

    The version is read lazily from the installed package metadata so
    importing this module never triggers a circular import against
    the `earthlens.core` package surface. The string is deliberately **non-Mozilla**:
    the DIGITAL.CSIC Anubis anti-bot wall (SPEIbase) blocks browser-like
    `User-Agent`s, and several upstreams throttle the bare python-requests
    default.

    Returns:
        The default agent string, e.g. `"earthlens/0.38.0"` (or
        `"earthlens/unknown"` when the package metadata is unavailable).
    """
    from earthlens.core import __version__
    return f"earthlens/{__version__}"


class RequestsGet:
    """Session-like adapter routing every call through the `requests` module.

    Passed as `HttpClient(session=RequestsGet())` by the backends that want a
    fresh connection per call (no pooled `requests.Session`) rather than
    connection reuse. It re-imports `requests` on every call so it dispatches
    through whatever `requests` is current — a test that monkeypatches
    `requests.get` (under any import alias; they all reference the one module)
    or injects a fake `requests` module via `sys.modules` still drives the
    transport. One shared class instead of the shim re-declared verbatim in a
    dozen backends.
    """

    def get(self, url: str, **kwargs: Any) -> Any:
        """Issue a `GET` via the current `requests.get`."""
        import requests

        return requests.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """Issue a `POST` via the current `requests.post`."""
        import requests

        return requests.post(url, **kwargs)


class HttpClient:
    """Reusable HTTP transport: session, headers, timeout, retry, download.

    Wraps a :class:`requests.Session`, attaching default headers (a
    non-Mozilla `User-Agent` and `Accept-Encoding: gzip, deflate`) to
    every request and applying a shared timeout. The verbs
    (:meth:`get`, :meth:`post`, :meth:`request`) route through a
    `Retry-After`-aware back-off loop; :meth:`get_json` decodes the JSON
    body and :meth:`download` streams a response to disk. Both the
    session and the sleep function are injectable so the client is fully
    unit-testable without a live network.

    Default headers set at construction are merged with (and overridden
    by) any per-request `headers=`, so a backend expresses its quirks —
    openaq's `X-API-Key`, a descriptive osm contact `User-Agent` — without
    subclassing.

    Attributes:
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retries on a retryable status before raising.
        backoff_factor: Base seconds for exponential back-off when no
            `Retry-After` header is present.
        status_forcelist: HTTP statuses that trigger a retry.
        max_backoff: Ceiling in seconds on any single retry wait.
        retry_on_exceptions: Transport exception types that trigger a retry.
        raise_for_status: Whether the final response is `raise_for_status`-ed.
        min_interval: Minimum seconds between consecutive requests.

    Examples:
        - The default agent is non-Mozilla and version-stamped:
            ```python
            >>> from earthlens.base.http import HttpClient
            >>> HttpClient().default_headers["User-Agent"].startswith("earthlens/")
            True

            ```
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        status_forcelist: tuple[int, ...] = DEFAULT_STATUS_FORCELIST,
        max_backoff: float | None = DEFAULT_MAX_BACKOFF,
        retry_on_exceptions: tuple[type[BaseException], ...] = (),
        retry_predicate: Callable[[requests.Response], bool] | None = None,
        raise_for_status: bool = True,
        min_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a client with default headers, timeout, and retry policy.

        Args:
            session: An existing :class:`requests.Session` to reuse.
                Defaults to a fresh session. Injectable so tests can
                supply a fake transport.
            user_agent: The default `User-Agent` header value. Defaults
                to `earthlens/{version}` (non-Mozilla by design; see
                :func:`_default_user_agent`).
            headers: Extra default headers merged onto every request
                (e.g. `{"X-API-Key": ...}`). Override per call with a
                request-level `headers=`.
            timeout: Per-request timeout in seconds.
            max_retries: Maximum retries on a retryable status before the
                last response's error is raised.
            backoff_factor: Base seconds for exponential back-off when a
                response carries no `Retry-After` header.
            status_forcelist: HTTP statuses that trigger a retry.
            max_backoff: Ceiling in seconds on any single retry wait, so a
                large `Retry-After` cannot pin the thread indefinitely.
                `None` disables the cap.
            retry_on_exceptions: Exception types that also trigger a retry
                when raised by the transport (e.g.
                `(requests.ConnectionError, requests.Timeout)`). Empty
                (the default) retries on status only, never on a raised
                exception.
            retry_predicate: An optional callback `(response) -> bool`
                that, when it returns `True`, marks a response retryable
                even if its status is not in `status_forcelist` (e.g. a
                `200` whose body signals a rate-limit).
            raise_for_status: Whether to call `raise_for_status` on the
                final response. `False` returns the response unraised so
                the caller can inspect the status itself (e.g. to redact a
                secret-bearing URL from the error, or to branch on a
                `4xx`). Overridable per request.
            min_interval: Minimum seconds between consecutive requests
                (a proactive client-side rate limit). `0.0` (default)
                disables throttling.
            clock: Monotonic clock used for the `min_interval` throttle.
                Injectable so tests drive it deterministically.
            sleep: The sleep function used between retries and for the
                throttle. Defaults to :func:`time.sleep`; injectable so
                tests run without real delays.
        """
        self._session = session if session is not None else requests.Session()
        self._user_agent = user_agent or _default_user_agent()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.status_forcelist = tuple(status_forcelist)
        self.max_backoff = max_backoff
        self.retry_on_exceptions = tuple(retry_on_exceptions)
        self._retry_predicate = retry_predicate
        self.raise_for_status = raise_for_status
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None
        self._default_headers: dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            self._default_headers.update(headers)

    @property
    def default_headers(self) -> dict[str, str]:
        """Return a copy of the headers merged onto every request."""
        return dict(self._default_headers)

    @property
    def session(self) -> requests.Session:
        """Return the underlying :class:`requests.Session`."""
        return self._session

    def _merge_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        """Merge per-request `headers` over the client's defaults."""
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        return merged

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        raise_for_status: bool | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request with the default headers, timeout, and retry.

        Args:
            method: HTTP verb (`"GET"`, `"POST"`, ...).
            url: Absolute request URL.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds). Defaults to
                the client's `timeout`.
            raise_for_status: Per-request override of the client's
                `raise_for_status` policy. `None` (default) uses the
                client setting.
            **kwargs: Extra keyword arguments forwarded to `requests`
                (`params`, `data`, `json`, `stream`, ...).

        Returns:
            requests.Response: The response (after `raise_for_status`
                unless it is disabled).

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted (when `raise_for_status`
                is on).
        """
        merged = self._merge_headers(headers)
        effective_timeout = self.timeout if timeout is None else timeout
        return self._request_with_retry(
            method,
            url,
            headers=merged,
            timeout=effective_timeout,
            raise_for_status=raise_for_status,
            **kwargs,
        )

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `GET` request. See :meth:`request` for arguments."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `POST` request. See :meth:`request` for arguments."""
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """Send a `GET` request and decode the JSON response body.

        Convenience over :meth:`get` for the REST endpoints that return
        JSON envelopes.

        Args:
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to :meth:`get`
                (`params`, `headers`, `timeout`, ...).

        Returns:
            The parsed JSON body (typically a `dict` or `list`).

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted.
        """
        return self.get(url, **kwargs).json()

    def stream(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a streaming `GET` (`stream=True`), retry-wrapped.

        Returns the open response without consuming its body, so the
        caller can iterate `iter_content`. Retries follow the same
        `Retry-After`/back-off policy as the other verbs; the retry
        decision reads only the status line, never the body.

        Args:
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to :meth:`get`.

        Returns:
            requests.Response: The open streaming response.
        """
        return self.get(url, stream=True, **kwargs)

    def _stream_to_file(
        self,
        response: requests.Response,
        dest: Path,
        *,
        chunk: int,
        progress: bool,
        desc: str,
    ) -> None:
        """Write a streaming response's body to `dest` with a `tqdm` bar.

        Args:
            response: The open streaming response.
            dest: The file to write (typically a temp `.part` path).
            chunk: Streaming block size in bytes.
            progress: Whether to show the progress bar.
            desc: The bar label (the final file name).
        """
        total = _progress_total(response.headers)
        bar = tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            disable=not progress,
            desc=desc,
        )
        try:
            with open(dest, "wb") as handle:
                for block in response.iter_content(chunk_size=chunk):
                    if block:
                        handle.write(block)
                        bar.update(len(block))
        finally:
            bar.close()

    def download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk: int = DEFAULT_CHUNK_SIZE,
        progress: bool = True,
        atomic: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Path:
        """Stream `url` to `dest` atomically, optionally showing a `tqdm` bar.

        Absorbs the chunk-loop the streamed-download backends each
        re-implement: streams with `stream=True`, sizes a progress bar
        from `Content-Length` when present, and writes 1 MiB blocks. When
        `atomic` (the default) it writes to a sibling `<dest>.part` and
        renames on success, and it removes the temp on any failure — so a
        crashed or interrupted download never leaves a truncated `dest`.
        The whole download is retry-wrapped: a status in `status_forcelist`
        or an exception in `retry_on_exceptions` retries the attempt (after
        cleaning the temp), honouring the `Retry-After`/back-off policy.

        Args:
            url: Absolute request URL.
            dest: Output file path. Parent directories are created.
            chunk: Streaming block size in bytes (default 1 MiB).
            progress: Show a `tqdm` progress bar. `False` (or a
                non-interactive / test context) suppresses it.
            atomic: Write to `<dest>.part` then rename on success, cleaning
                up the temp on failure. `False` writes straight to `dest`.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds).
            **kwargs: Extra keyword arguments forwarded to `requests`.

        Returns:
            Path: The `dest` path the bytes were written to.

        Raises:
            requests.HTTPError: On an error status — `download` always
                calls `raise_for_status` (the client's `raise_for_status`
                flag governs the verb methods, not `download`; a file
                fetch never keeps an error body). Note the resulting
                `HTTPError` is itself subject to `retry_on_exceptions`:
                a client whose `retry_on_exceptions` includes a supertype
                of `requests.HTTPError` (e.g. `requests.RequestException`,
                as ghsl/glaciers pass) will **retry** an error status
                before raising it, mirroring their old download loops.
                Also the last transport exception after the retry budget
                is exhausted.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part") if atomic else dest
        merged = self._merge_headers(headers)
        effective_timeout = self.timeout if timeout is None else timeout
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._send(
                    "GET",
                    url,
                    stream=True,
                    headers=merged,
                    timeout=effective_timeout,
                    **kwargs,
                )
                try:
                    retryable = response.status_code in self.status_forcelist or (
                        self._retry_predicate is not None
                        and self._retry_predicate(response)
                    )
                    if retryable and attempt < self.max_retries:
                        retry_after = _parse_retry_after(
                            response.headers.get("Retry-After")
                        )
                        wait = self._backoff_wait(retry_after, attempt)
                        logger.warning(
                            f"HTTP {response.status_code} on {_redact_url(url)}; retry "
                            f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                        )
                        self._sleep(wait)
                        attempt += 1
                        continue
                    response.raise_for_status()
                    self._stream_to_file(
                        response, tmp, chunk=chunk, progress=progress, desc=dest.name
                    )
                finally:
                    response.close()
            except self.retry_on_exceptions as exc:
                tmp.unlink(missing_ok=True)
                if attempt >= self.max_retries:
                    raise
                wait = self._backoff_wait(None, attempt)
                logger.warning(
                    f"{type(exc).__name__} on {_redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            if atomic:
                # Guard the rename too, so the "removes the temp on any
                # failure" promise holds if the final replace fails.
                try:
                    tmp.replace(dest)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
            return dest

    def _throttle(self) -> None:
        """Sleep so consecutive requests are >= `min_interval` apart.

        A no-op when `min_interval` is `0`. Uses the injected monotonic
        `clock`, records the send time, and sleeps via the injected
        `sleep` so tests drive the rate limit deterministically.
        """
        if self.min_interval <= 0:
            return
        if self._last_request is not None:
            remaining = self.min_interval - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()

    def _backoff_wait(self, retry_after: float | None, attempt: int) -> float:
        """Compute one retry wait: `Retry-After` else exponential back-off.

        Applies the `max_backoff` ceiling and a non-negative floor.

        Args:
            retry_after: Parsed `Retry-After` seconds, or `None`.
            attempt: The zero-based attempt index.

        Returns:
            The clamped wait in seconds.
        """
        wait = (
            retry_after
            if retry_after is not None
            else self.backoff_factor * (2**attempt)
        )
        if self.max_backoff is not None:
            wait = min(wait, self.max_backoff)
        return max(0.0, wait)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        raise_for_status: bool | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request, retrying statuses/exceptions with back-off.

        Retries while the attempt budget holds and either the response
        status is in `status_forcelist`, the `retry_predicate` marks the
        response retryable, or the transport raised one of
        `retry_on_exceptions`. Waits `Retry-After` seconds when present
        and numeric, otherwise `backoff_factor * 2**attempt` (capped by
        `max_backoff`). Honours the `min_interval` throttle before every
        send. Once non-retryable or exhausted, the response is
        `raise_for_status`-ed unless that is disabled, then returned.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            raise_for_status: Per-request override; `None` uses the
                client policy.
            **kwargs: Keyword arguments forwarded to the session.

        Returns:
            requests.Response: The final response.

        Raises:
            requests.HTTPError: On a non-retryable error status when
                `raise_for_status` is on.
            BaseException: The last transport exception, re-raised after
                the `retry_on_exceptions` budget is exhausted.
        """
        effective_raise = (
            self.raise_for_status if raise_for_status is None else raise_for_status
        )
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._send(method, url, **kwargs)
            except self.retry_on_exceptions as exc:
                if attempt >= self.max_retries:
                    raise
                wait = self._backoff_wait(None, attempt)
                logger.warning(
                    f"{type(exc).__name__} on {_redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            retryable = response.status_code in self.status_forcelist or (
                self._retry_predicate is not None and self._retry_predicate(response)
            )
            if retryable and attempt < self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = self._backoff_wait(retry_after, attempt)
                logger.warning(
                    f"HTTP {response.status_code} on {_redact_url(url)}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                # Release the (possibly streamed) connection before retrying;
                # a stream=True body is otherwise never consumed and its socket
                # leaks out of the pool.
                response.close()
                self._sleep(wait)
                attempt += 1
                continue
            if effective_raise:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    # Close the final errored response too — for a streamed
                    # request the caller never receives it, so nothing else would.
                    response.close()
                    raise
            return response

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Dispatch to the session's verb method, falling back to `request`.

        Routing `GET` to `session.get` (rather than a generic
        `session.request`) keeps drop-in fake transports that implement
        only `get()` working — the shape the migrated backends' tests use.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to the session call.

        Returns:
            requests.Response: The raw response (no status check).
        """
        verb = getattr(self._session, method.lower(), None)
        if callable(verb):
            return verb(url, **kwargs)
        return self._session.request(method, url, **kwargs)
