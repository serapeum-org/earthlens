"""Pure request/parsing helpers for the FIRMS backend.

Holds the FIRMS-specific logic that needs **no GIS container** so it can
be unit-tested without importing pyramids: the ≤10-day window chunking
(`G1`), the reactive quota back-off (`G2`), and the
error-as-HTTP-200-text-body classification (`G6`). Keeping these here
(rather than inline in `backend.py`, which imports
:mod:`earthlens.firms.events` and therefore pyramids) mirrors the GEE
backend's `_helpers.py` split and makes the trickiest FIRMS behaviour
directly testable.

FIRMS has two engineering wrinkles these helpers absorb:

* The area endpoint caps `day_range` at 10 days and serves one sensor
  per request, so a long window is walked in ≤10-day steps
  (:func:`chunk_windows`).
* FIRMS frequently returns *errors* with HTTP status 200 and a
  plain-text body (`Invalid MAP_KEY.`, a transaction-limit message, an
  `Invalid ...` parameter message). :func:`classify_body` sniffs the
  body so such a response is never fed to `pandas.read_csv`, and
  :func:`firms_get` retries the quota case with a capped back-off.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable, Literal

#: A FIRMS area request covers at most this many days.
MAX_DAY_RANGE = 10

#: Body kinds :func:`classify_body` distinguishes.
BodyKind = Literal["csv", "auth", "quota", "error"]


def chunk_windows(
    start: dt.date,
    end: dt.date,
    max_days: int = MAX_DAY_RANGE,
) -> list[tuple[dt.date, int]]:
    """Split an inclusive `[start, end]` window into ≤`max_days` chunks.

    FIRMS caps `day_range` at 10 and treats `start_date` + `day_range`
    inclusively, so a 1-day window is `day_range=1`. Each chunk is
    `(chunk_start, day_range)` where `chunk_start = start + max_days·i`
    and `day_range` is `max_days` except for the final remainder.

    Args:
        start: Inclusive first date of the window.
        end: Inclusive last date of the window.
        max_days: Per-request day cap (10 for FIRMS).

    Returns:
        list[tuple[date, int]]: One `(chunk_start, day_range)` per
            request, in chronological order. A single-day window yields
            `[(start, 1)]`.

    Raises:
        ValueError: If `end` is before `start`, or `max_days < 1`.

    Examples:
        - A 1-day window is a single `day_range=1` request:
            ```python
            >>> import datetime as dt
            >>> from earthlens.firms._helpers import chunk_windows
            >>> chunk_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 1))
            [(datetime.date(2024, 1, 1), 1)]

            ```
        - A 25-day window chunks into 10 / 10 / 5:
            ```python
            >>> import datetime as dt
            >>> from earthlens.firms._helpers import chunk_windows
            >>> [dr for _, dr in chunk_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 25))]
            [10, 10, 5]

            ```
    """
    if max_days < 1:
        raise ValueError(f"max_days must be >= 1, got {max_days}.")
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start}).")
    total_days = (end - start).days + 1
    chunks: list[tuple[dt.date, int]] = []
    offset = 0
    while offset < total_days:
        chunk_start = start + dt.timedelta(days=offset)
        day_range = min(max_days, total_days - offset)
        chunks.append((chunk_start, day_range))
        offset += day_range
    return chunks


def classify_body(text: str) -> BodyKind:
    """Classify a FIRMS response body before it reaches `pandas.read_csv`.

    FIRMS returns some errors as HTTP 200 with a plain-text body, so the
    status code alone cannot be trusted. A valid CSV begins with the
    `latitude,longitude,...` header; anything else is an error message
    whose wording selects the failure type.

    Args:
        text: The raw response body.

    Returns:
        BodyKind: `"csv"` for a real CSV payload; `"auth"` for a
            bad-key message; `"quota"` for a transaction/rate-limit
            message; `"error"` for any other non-CSV body.

    Examples:
        - A real CSV header:
            ```python
            >>> from earthlens.firms._helpers import classify_body
            >>> classify_body("latitude,longitude,bright_ti4\\n1,2,300")
            'csv'

            ```
        - The bad-key and quota sentinels:
            ```python
            >>> from earthlens.firms._helpers import classify_body
            >>> classify_body("Invalid MAP_KEY.")
            'auth'
            >>> classify_body("You have exceeded your transaction limit.")
            'quota'
            >>> classify_body("Invalid coordinates")
            'error'

            ```
    """
    stripped = text.lstrip()
    if stripped[:8].lower() == "latitude":
        return "csv"
    lowered = stripped.lower()
    if "map_key" in lowered or "invalid key" in lowered or "map key" in lowered:
        return "auth"
    if any(
        token in lowered
        for token in ("transaction", "rate limit", "rate-limit", "quota", "exceeded")
    ):
        return "quota"
    return "error"


def firms_get(
    url: str,
    *,
    timeout: float,
    get: Callable[..., Any],
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = 5,
    backoff_factor: float = 1.0,
) -> Any:
    """GET a FIRMS URL, retrying the quota case with capped back-off.

    Issues `get(url, timeout=timeout)` and, when the response is a quota
    signal — HTTP 429, or an HTTP-200 body that :func:`classify_body`
    flags as `"quota"` — waits `backoff_factor · 2**attempt` seconds and
    retries, up to `max_retries` times. Any other response (a real CSV,
    a bad-key body, a generic error body, a non-quota HTTP error) is
    returned to the caller verbatim for classification; this helper does
    not raise on those. The `sleep` and `get` callables are injected so
    tests run without real waits or network.

    Args:
        url: The fully-formed FIRMS request URL (already carries the
            `MAP_KEY` path segment).
        timeout: Per-request timeout in seconds.
        get: The HTTP getter (`requests.get` in production).
        sleep: Sleep function used between retries; injectable for tests.
        max_retries: Maximum quota retries before returning the last
            response.
        backoff_factor: Base seconds for the exponential back-off.

    Returns:
        The final response object from `get`.
    """
    response = get(url, timeout=timeout)
    attempt = 0
    while attempt < max_retries and _is_quota(response):
        sleep(backoff_factor * (2**attempt))
        attempt += 1
        response = get(url, timeout=timeout)
    return response


def _is_quota(response: Any) -> bool:
    """Return `True` when a response is a FIRMS quota signal.

    A quota signal is HTTP 429, or an HTTP-200 text body that
    :func:`classify_body` flags as `"quota"`.

    Args:
        response: A response object exposing `status_code` and `text`.

    Returns:
        bool: `True` for a quota/rate-limit response.
    """
    if getattr(response, "status_code", None) == 429:
        return True
    text = getattr(response, "text", "") or ""
    return classify_body(text) == "quota"
