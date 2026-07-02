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
mounted on an `HTTPAdapter`) generalising the reference
`earthlens.openaq.client.OpenaqClient._request` loop: `Retry-After`
first, else `backoff_factor * 2**attempt`. Both the transport
(`session=`) and the wait (`sleep=`) are injectable, so the whole client
is unit-testable with a fake transport — no live network, no real
delays.

`requests` and `tqdm` are already core earthlens dependencies, so this
module adds none. The public import is
`from earthlens.base.http import HttpClient`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

#: Per-request timeout (seconds) applied when a call passes no `timeout`.
DEFAULT_TIMEOUT = 60.0

#: Maximum retries for a retryable status before the last response's
#: error is raised.
DEFAULT_MAX_RETRIES = 5

#: Base seconds for exponential back-off when a response carries no
#: `Retry-After` header (wait = `backoff_factor * 2**attempt`).
DEFAULT_BACKOFF_FACTOR = 1.0

#: HTTP statuses that trigger a retry. `429` (rate-limited) plus the
#: transient `5xx` gateway/unavailable family.
DEFAULT_STATUS_FORCELIST: tuple[int, ...] = (429, 500, 502, 503, 504)


def _default_user_agent() -> str:
    """Return the default `User-Agent`, `earthlens/{version}`.

    The version is read lazily from the installed package metadata so
    importing this module never triggers a circular import against
    `earthlens.__init__`. The string is deliberately **non-Mozilla**:
    the DIGITAL.CSIC Anubis anti-bot wall (SPEIbase) blocks browser-like
    `User-Agent`s, and several upstreams throttle the bare python-requests
    default.

    Returns:
        The default agent string, e.g. `"earthlens/0.38.0"` (or
        `"earthlens/unknown"` when the package metadata is unavailable).
    """
    from earthlens import __version__

    return f"earthlens/{__version__}"


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
            sleep: The sleep function used between retries. Defaults to
                :func:`time.sleep`; injectable so tests run without real
                delays.
        """
        self._session = session if session is not None else requests.Session()
        self._user_agent = user_agent or _default_user_agent()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.status_forcelist = tuple(status_forcelist)
        self._sleep = sleep
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
        **kwargs: Any,
    ) -> requests.Response:
        """Send one request with the default headers, timeout, and retry.

        Args:
            method: HTTP verb (`"GET"`, `"POST"`, ...).
            url: Absolute request URL.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds). Defaults to
                the client's `timeout`.
            **kwargs: Extra keyword arguments forwarded to `requests`
                (`params`, `data`, `json`, `stream`, ...).

        Returns:
            requests.Response: The successful response (after
                `raise_for_status`).

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted.
        """
        merged = self._merge_headers(headers)
        effective_timeout = self.timeout if timeout is None else timeout
        return self._request_with_retry(
            method, url, headers=merged, timeout=effective_timeout, **kwargs
        )

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `GET` request. See :meth:`request` for arguments."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a `POST` request. See :meth:`request` for arguments."""
        return self.request("POST", url, **kwargs)

    def _request_with_retry(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """Send one request and raise for status (single-shot).

        The retry/back-off loop is layered on in `C2`; this base form
        issues a single request and raises on any error status.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to the session.

        Returns:
            requests.Response: The response after `raise_for_status`.
        """
        response = self._send(method, url, **kwargs)
        response.raise_for_status()
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
