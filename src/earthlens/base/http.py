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
from pathlib import Path
from typing import Any

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

#: HTTP statuses that trigger a retry. `429` (rate-limited) plus the
#: transient `5xx` gateway/unavailable family.
DEFAULT_STATUS_FORCELIST: tuple[int, ...] = (429, 500, 502, 503, 504)

#: Streaming chunk size (bytes) for :meth:`HttpClient.download` — 1 MiB.
DEFAULT_CHUNK_SIZE = 1 << 20


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    Handles the integer-seconds form servers commonly return (e.g.
    `Retry-After: 5`). A missing or non-numeric value yields `None` so
    the caller falls back to exponential back-off. The HTTP-date form is
    not parsed (no surveyed backend emits it); it also yields `None`.

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable.

    Examples:
        - A numeric value parses to seconds; junk yields `None`:
            ```python
            >>> from earthlens.base.http import _parse_retry_after
            >>> _parse_retry_after("5")
            5.0
            >>> _parse_retry_after(None) is None
            True
            >>> _parse_retry_after("soon") is None
            True

            ```
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    def download(
        self,
        url: str,
        dest: str | Path,
        *,
        chunk: int = DEFAULT_CHUNK_SIZE,
        progress: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Path:
        """Stream `url` to `dest`, optionally showing a `tqdm` bar.

        Absorbs the chunk-loop the streamed-download backends each
        re-implement: streams with `stream=True`, sizes a progress bar
        from `Content-Length` when present, writes 1 MiB blocks to
        `dest` (creating parent directories), and returns the path. The
        initial response is retry-wrapped via :meth:`stream`.

        Args:
            url: Absolute request URL.
            dest: Output file path. Parent directories are created.
            chunk: Streaming block size in bytes (default 1 MiB).
            progress: Show a `tqdm` progress bar. `False` (or a
                non-interactive / test context) suppresses it.
            headers: Per-request headers merged over the client defaults.
            timeout: Per-request timeout override (seconds).
            **kwargs: Extra keyword arguments forwarded to `requests`.

        Returns:
            Path: The `dest` path the bytes were written to.

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = self.stream(url, headers=headers, timeout=timeout, **kwargs)
        raw_length = response.headers.get("Content-Length")
        total = (
            int(raw_length) if raw_length is not None and raw_length.isdigit() else None
        )
        bar = tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            disable=not progress,
            desc=dest.name,
        )
        try:
            with open(dest, "wb") as handle:
                for block in response.iter_content(chunk_size=chunk):
                    if block:
                        handle.write(block)
                        bar.update(len(block))
        finally:
            bar.close()
            response.close()
        return dest

    def _request_with_retry(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """Send one request, retrying retryable statuses with back-off.

        Retries while the response status is in `status_forcelist` and
        the attempt budget is not spent: it waits `Retry-After` seconds
        when the header is present and numeric, otherwise
        `backoff_factor * 2**attempt`. Once the status is non-retryable
        or the retries are exhausted, `raise_for_status` decides success
        or error.

        Args:
            method: HTTP verb.
            url: Absolute request URL.
            **kwargs: Keyword arguments forwarded to the session.

        Returns:
            requests.Response: The response after `raise_for_status`.

        Raises:
            requests.HTTPError: On a non-retryable error status, or after
                the retryable status is exhausted.
        """
        attempt = 0
        while True:
            response = self._send(method, url, **kwargs)
            if (
                response.status_code in self.status_forcelist
                and attempt < self.max_retries
            ):
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after
                    if retry_after is not None
                    else self.backoff_factor * (2**attempt)
                )
                logger.warning(
                    f"HTTP {response.status_code} on {url!r}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
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
