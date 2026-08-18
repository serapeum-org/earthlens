"""Availability classification for the GDACS SEARCH backend.

Factored out of `gdacs/backend.py` so the backend only routes: this module owns
the typed `GdacsUnavailableError`, the retry-policy constants the backend hands
to `HttpClient`, and the `service_failure_reason` classifier that decides whether
a failed SEARCH call was the *service* being unavailable (retry, then skip a live
test) or a genuine request error (fail).

The one GDACS-specific twist lives here. GDACS SEARCH is observed to answer a
*well-formed* query with a spurious `400 Bad Request` under load — issue #929,
where the exact failing URL returned `200` on an immediate re-request and 6/6 on
repeat — so a `400` is treated as retry-worthy (the backend retries it) and, only
if it survives every retry, as an availability failure. A genuine
query-composition regression does not reach this classifier: it is caught
deterministically and offline by the gdacs unit tests (`test_forwards_params`
asserts the composed `fromDate` / `toDate` / `eventlist` / `alertlevel`).

Mirrors the OSM backend's `OhsomeUnavailableError` / `ohsome_http_status` pair and
the EMODnet-WCS `is_wcs_service_failure` classifier: a live e2e test catches
`GdacsUnavailableError` and skips, so a transient upstream condition reports
`skipped`, not `failed`.
"""

from __future__ import annotations

import re

import requests

#: HTTP statuses that mark the GDACS *service* — not the request — as the
#: problem. The transient gateway / rate-limit family (`408` request timeout,
#: `425` too early, `429` rate limited, and the `5xx` family) plus `400`: GDACS
#: SEARCH returns a spurious `400` on a well-formed query under load (issue #929),
#: so a `400` is retry-worthy here and, if it persists, an availability failure.
#: The backend both retries these and re-raises a survivor as
#: :class:`GdacsUnavailableError`; every other status (`403` / `404` / ...) stays
#: an authoritative failure.
GDACS_SERVICE_STATUSES: frozenset[int] = frozenset(
    {400, 408, 425, 429, 500, 502, 503, 504}
)

#: The same set as a sorted tuple, for `HttpClient(status_forcelist=...)`.
GDACS_RETRY_STATUSES: tuple[int, ...] = tuple(sorted(GDACS_SERVICE_STATUSES))

#: Retries the backend allows before a retry-worthy status / transport error is
#: re-raised. Small: GDACS's spurious `400`s and gateway hiccups clear within a
#: couple of back-off waits (issue #929), and a live test should not stall long
#: on a genuinely down service before it skips.
GDACS_MAX_RETRIES: int = 3

#: Transport exceptions that trigger a retry (and, if they survive, a skip): a
#: dropped connection or a read timeout — the second failure mode in issue #929.
GDACS_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
)

#: `NNN Server Error` / `NNN Client Error` at the head of a `requests.HTTPError`
#: message — the fallback when the exception carries no `response` object (as a
#: hand-built `requests.HTTPError("500 Server Error")` in a test does).
_STATUS_IN_MESSAGE = re.compile(
    r"\s*(\d{3})\s+(?:server|client)\s+error", re.IGNORECASE
)


class GdacsUnavailableError(RuntimeError):
    """The GDACS SEARCH feed was unavailable after the backend's retries.

    Raised by the GDACS backend when a combined SEARCH request fails for a
    reason that is the *service*, not the request: a dropped connection, a read
    timeout, or a retry-worthy status (`400` / `408` / `425` / `429` / `5xx`)
    that outlived every retry. Carries the originating HTTP `status_code` when
    one is discernible, so a caller — a live e2e test especially — can tell a
    transient upstream condition apart from a genuine request error and skip
    rather than fail. The composed query is validated offline by the gdacs unit
    tests, so a `400` reaching here is a spurious upstream `400` (issue #929),
    not a malformed request.
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


def gdacs_http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status behind a failed SEARCH call.

    Reads the status from a `requests`-style `response.status_code` when the
    exception carries a response, and otherwise parses a leading
    `NNN Server/Client Error` out of the message — the shape a
    `raise_for_status`-built `requests.HTTPError` has before it is attached to a
    response. Walks the `__cause__` / `__context__` chain (cycle-safe) so a
    wrapped error still yields its status.

    Args:
        exc: The exception raised by a SEARCH call.

    Returns:
        int | None: The HTTP status code, or `None` when none is discoverable
            (for example a bare connection error or read timeout).

    Examples:
        - The status is recovered from a bare `raise_for_status` message:
            ```python
            >>> import requests
            >>> from earthlens.gdacs._helpers import gdacs_http_status
            >>> gdacs_http_status(requests.HTTPError("400 Client Error: Bad Request"))
            400
            >>> gdacs_http_status(requests.ConnectionError("boom")) is None
            True

            ```
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
        match = _STATUS_IN_MESSAGE.match(str(current))
        if match is not None:
            return int(match.group(1))
        current = current.__cause__ or current.__context__
    return None


def service_failure_reason(exc: BaseException) -> str | None:
    """Classify `exc` as a GDACS *availability* failure, or not.

    Returns a short reason string when the failure is the service being
    unreachable or refusing the request in a retry-worthy way — a connection or
    timeout error, or a status in :data:`GDACS_SERVICE_STATUSES` — and `None`
    for anything else (a `403` / `404`, a JSON-decode `ValueError`, ...), which
    the backend then propagates unchanged.

    Args:
        exc: The exception raised by a SEARCH call, after the backend's retries.

    Returns:
        str | None: A human-readable reason when `exc` is an availability
            failure, else `None`.
    """
    status = gdacs_http_status(exc)
    if status is not None:
        return f"HTTP {status}" if status in GDACS_SERVICE_STATUSES else None
    if isinstance(exc, GDACS_RETRY_EXCEPTIONS):
        return f"upstream unreachable ({type(exc).__name__})"
    return None
