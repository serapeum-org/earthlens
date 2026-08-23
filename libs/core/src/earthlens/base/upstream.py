"""Shared upstream-availability error and HTTP-status extraction.

One typed error and one set of status helpers, so the provider backends (gdacs,
osm, ecmwf, bathymetry) and the `earthlens.testing` skip classifier stop each
re-implementing the exception-chain walk, the `bool`-is-`int` guard, and the
`NNN Server/Client Error` message parse. Each provider raises the shared
:class:`UpstreamUnavailableError` (subclassing only where the provider name aids
the traceback) and composes these helpers rather than restating them.

The split mirrors how a status is actually recovered:

* :func:`exception_chain` walks `__cause__` / `__context__` (an SDK usually
  buries the `requests` error that carries the status);
* :func:`response_status` reads one exception structurally (a
  `urllib.error.HTTPError.code` or a `requests` `.response.status_code`);
* :func:`status_in_message` is the text fallback for a `raise_for_status`
  message before a response is attached, with an `anchored` switch so a caller
  that also sees response-body echoes (a pytest-rewritten `AssertionError`) can
  refuse a status buried mid-string;
* :func:`http_status` composes the three into the common "status behind a
  failure" one-liner.
"""

from __future__ import annotations

import re
import urllib.error
from collections.abc import Iterator

__all__ = [
    "UpstreamUnavailableError",
    "exception_chain",
    "http_status",
    "is_http_status",
    "response_status",
    "status_in_message",
]


class UpstreamUnavailableError(RuntimeError):
    """An external service was unreachable or unusable after the client's retries.

    The shared base every provider's availability error subclasses. Raised when a
    request fails for a reason that is the *service*, not the request — a dropped
    connection, a read timeout, or a retry-worthy status (`408` / `425` / `429` /
    `5xx`, and the odd spurious `4xx`) that outlived the backend's own retries.
    Carries the originating HTTP `status_code` when one is discernible, so a
    caller — a live `e2e` test especially — can tell a transient upstream
    condition apart from a genuine request error and skip rather than fail. A
    `RuntimeError`, so a broad transport-failure `except RuntimeError` still
    catches it.

    Examples:
        - The error carries the status a caller branches on:
            ```python
            >>> from earthlens.base import UpstreamUnavailableError
            >>> err = UpstreamUnavailableError("service unavailable", status_code=503)
            >>> err.status_code
            503
            >>> str(err)
            'service unavailable'

            ```
        - A transport failure carries no status:
            ```python
            >>> from earthlens.base import UpstreamUnavailableError
            >>> UpstreamUnavailableError("connection dropped").status_code is None
            True

            ```
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Store the actionable message and the originating HTTP status.

        Args:
            message: Human-facing explanation — what happened and what to do.
            status_code: The HTTP status that triggered it, or `None` when the
                failure was a transport error (no status) or the status could
                not be recovered from the exception.
        """
        super().__init__(message)
        self.status_code = status_code


def is_http_status(value: object) -> bool:
    """Return whether `value` is a real integer HTTP status (not a `bool`).

    `bool` is a subclass of `int`, so a plain `isinstance(value, int)` would
    accept `True` / `False` and report a nonsensical status `1` / `0`; this
    excludes them.

    Args:
        value: A candidate status pulled off an exception or a response.

    Returns:
        `True` when `value` is an `int` and not a `bool`.

    Examples:
        - A real status passes; a `bool` or `None` does not:
            ```python
            >>> from earthlens.base import is_http_status
            >>> is_http_status(503)
            True
            >>> is_http_status(True)
            False
            >>> is_http_status(None)
            False

            ```
    """
    return isinstance(value, int) and not isinstance(value, bool)


def exception_chain(exc: BaseException) -> Iterator[Exception]:
    """Yield each `Exception` in `exc`'s `__cause__` / `__context__` chain, cycle-safe.

    Yields only real `Exception` links: a `KeyboardInterrupt` / `SystemExit`
    caught in the chain carries no HTTP status and must never be message-sniffed
    for one, so it is skipped — but the walk still traverses *through* it, so a
    status deeper in the chain is not lost. Honours `raise … from None`: an
    explicit `__cause__` wins, otherwise the implicit `__context__` is followed
    only when the author did not suppress it (matching stdlib `traceback`), so a
    deliberately surfaced failure is not reclassified through a context it asked
    to hide. Cycle-guarded, so a self-referential chain terminates.

    Args:
        exc: The exception to walk.

    Yields:
        Each `Exception` in the chain, most recent first (non-`Exception` links
        are traversed but not yielded).

    Examples:
        - The walk yields the wrapper then its explicit cause:
            ```python
            >>> from earthlens.base import exception_chain
            >>> cause = ValueError("root")
            >>> try:
            ...     raise RuntimeError("wrapper") from cause
            ... except RuntimeError as exc:
            ...     [str(link) for link in exception_chain(exc)]
            ['wrapper', 'root']

            ```
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Exception):
            yield current
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__


def response_status(exc: BaseException) -> int | None:
    """Return the HTTP status one exception carries, read structurally.

    Reads the two SDK shapes directly — `urllib.error.HTTPError.code` and a
    `requests`-style `.response.status_code` (a `bool` is rejected, since it is
    an `int` subclass) — so a status is taken from structure rather than parsed
    out of free text (which a URL or a pixel count could spoof). Inspects only
    `exc` itself; use :func:`http_status` to walk a chain.

    Args:
        exc: A single exception to inspect.

    Returns:
        The status code, or `None` when `exc` carries none structurally.

    Examples:
        - A `requests`-style error exposes it on the response:
            ```python
            >>> from earthlens.base import response_status
            >>> class _Resp:
            ...     status_code = 503
            >>> class _Err(Exception):
            ...     response = _Resp()
            >>> response_status(_Err())
            503
            >>> response_status(ValueError("no status here")) is None
            True

            ```
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if is_http_status(code) else None


def status_in_message(text: str, *, anchored: bool = False) -> int | None:
    """Return the `NNN Server/Client Error` status named in `text`, if present.

    The fallback for a `requests.HTTPError` built by `raise_for_status` before a
    `response` is attached, whose message leads with `NNN Server Error`. With
    `anchored=True` the status must lead the message (`re.match` semantics) — the
    safe form for text that may echo a *response body* (for example a
    pytest-rewritten `AssertionError`), where a status buried mid-string is not
    authoritative. With `anchored=False` (the default) a status anywhere in the
    text matches (`re.search`) — for SDK wrappers like `cdsapi` that bury it.

    Args:
        text: The exception message to scan.
        anchored: Require the status at the start of the message when `True`.

    Returns:
        The status code, or `None` when the text names none.

    Examples:
        - A leading status is found either way:
            ```python
            >>> from earthlens.base import status_in_message
            >>> status_in_message("500 Server Error: boom")
            500
            >>> status_in_message("500 Server Error: boom", anchored=True)
            500

            ```
        - A buried status needs the unanchored (default) form:
            ```python
            >>> from earthlens.base import status_in_message
            >>> status_in_message("failed: 502 Server Error")
            502
            >>> status_in_message("failed: 502 Server Error", anchored=True) is None
            True

            ```
    """
    prefix = r"^\s*" if anchored else r"\b"
    match = re.search(
        prefix + r"(\d{3})\s+(?:server|client)\s+error", text, re.IGNORECASE
    )
    return int(match.group(1)) if match else None


def http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status behind a failure, walking the exception chain.

    Walks `exc` and its `__cause__` / `__context__` predecessors and returns the
    first status found — read structurally by :func:`response_status`, or failing
    that from a `NNN Server/Client Error` anywhere in a link's message
    (:func:`status_in_message`). A wrapped error — an SDK burying the `requests`
    failure that carries the response — still yields its status.

    Args:
        exc: The exception raised by a live call.

    Returns:
        The status code, or `None` when none is discernible (a transport drop
        carries none).

    Examples:
        - The status is recovered from a bare `raise_for_status` message:
            ```python
            >>> import requests
            >>> from earthlens.base import http_status
            >>> http_status(requests.HTTPError("400 Client Error: Bad Request"))
            400
            >>> http_status(requests.ConnectionError("boom")) is None
            True

            ```
    """
    for link in exception_chain(exc):
        found = response_status(link)
        if found is not None:
            return found
        found = status_in_message(str(link))
        if found is not None:
            return found
    return None
