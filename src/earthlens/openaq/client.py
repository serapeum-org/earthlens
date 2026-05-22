"""Thin `requests`-based HTTP client for the OpenAQ v3 API.

Owns the three concerns a pre-v1 SDK would hide: the `X-API-Key`
header, page-by-page iteration of the v3 list endpoints, and — the
backend's main risk — `429 Too Many Requests` handling with
`Retry-After`-aware exponential back-off. OpenAQ's free tier
rate-limits (historically ~60 req/min, ~2000/hour), and the
locations -> sensors -> measurements fan-out makes a continental bbox
hundreds-to-thousands of requests, so back-off is not optional.

This is the local `_request_with_backoff` substrate the plan's `R2`
finding settled on: the shared `earthlens.base.http.HttpClient` (the
planned foundation task) does not exist yet, so the client owns its
own retry loop. If that primitive lands later, this client can be
re-pointed at it without changing the backend.

`requests` is already a core earthlens dependency, so this client adds
none. The session is injectable (`session=`) so tests can drive the
client with a fake transport — no live network and no real sleeps
(pass `sleep=` to capture back-off waits).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from loguru import logger

#: OpenAQ v3 API base URL. All endpoint paths are joined onto this.
BASE_URL = "https://api.openaq.org/v3"

#: Rollup endpoints that filter on a calendar `date_from`/`date_to`
#: (date granularity) rather than `datetime_from`/`datetime_to`. The raw
#: `/measurements` and the `/hours` rollup take datetime filters; `/days`,
#: `/months`, and `/years` take date filters and *silently ignore* a
#: `datetime_*` filter (returning the sensor's full history). Verified
#: against the live v3 API on 2026-05-22.
_DATE_FILTER_ROLLUPS = frozenset({"days", "months", "years"})


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    OpenAQ returns `Retry-After` as an integer number of seconds. A
    missing or non-numeric value yields `None` so the caller falls
    back to exponential back-off.

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable.

    Examples:
        - A numeric value parses to seconds; junk yields `None`:
            ```python
            >>> from earthlens.openaq.client import _parse_retry_after
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


class OpenaqClient:
    """Minimal OpenAQ v3 client: auth header, pagination, back-off.

    Wraps a :class:`requests.Session`, attaching the `X-API-Key`
    header to every request and retrying on `429` with a
    `Retry-After`-aware exponential back-off. Exposes the two list
    endpoints the backend needs — `locations` and a sensor's
    measurements (raw or a server-side rollup) — plus a generic
    :meth:`paginate` over any v3 list resource.

    Attributes:
        max_retries: Maximum number of `429` retries before the last
            response's error is raised.
        backoff_factor: Base seconds for exponential back-off when no
            `Retry-After` header is present (wait = factor * 2**attempt).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        max_retries: int = 5,
        backoff_factor: float = 1.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a client bound to one API key.

        Args:
            api_key: The OpenAQ `X-API-Key` to attach to every request.
            session: An existing :class:`requests.Session` to reuse.
                Defaults to a fresh session. Injectable so tests can
                supply a fake transport.
            max_retries: Maximum `429` retries before raising.
            backoff_factor: Base seconds for exponential back-off when
                the response carries no `Retry-After` header.
            timeout: Per-request timeout in seconds.
            sleep: The sleep function used between retries. Defaults to
                :func:`time.sleep`; injectable so tests run without
                real delays.
        """
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._sleep = sleep

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET one page of `path`, retrying on `429`.

        Honours a `Retry-After` header when present, otherwise backs
        off exponentially (`backoff_factor * 2**attempt`). After
        `max_retries` exhausted `429`s, the final response's
        `raise_for_status` propagates. Any non-`429` HTTP error raises
        immediately.

        Args:
            path: Endpoint path relative to :data:`BASE_URL` (e.g.
                `"locations"`, `"sensors/42/days"`).
            params: Query parameters.

        Returns:
            dict[str, Any]: The parsed JSON body (the v3
                `{"meta": ..., "results": [...]}` envelope).

        Raises:
            requests.HTTPError: On a non-`429` error status, or after
                `max_retries` exhausted `429` responses.
        """
        url = f"{BASE_URL}/{path}"
        headers = {"X-API-Key": self._api_key}
        attempt = 0
        while True:
            response = self._session.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after
                    if retry_after is not None
                    else self.backoff_factor * (2**attempt)
                )
                logger.warning(
                    f"OpenAQ rate-limited (429) on {path!r}; retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            response.raise_for_status()
            return response.json()

    def paginate(
        self,
        path: str,
        params: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every result across pages of a v3 list endpoint.

        Walks `page=1, 2, ...` until a short page signals the end (or
        `max_items` is reached), yielding each element of the
        `results` array. The page size is taken from `params["limit"]`.

        Args:
            path: List-endpoint path relative to :data:`BASE_URL`.
            params: Query parameters; `limit` sets the page size.
            max_items: Stop after yielding this many results. `None`
                (default) means no cap — exhaust the endpoint.

        Yields:
            dict[str, Any]: One result object per element.
        """
        page = 1
        yielded = 0
        page_params = dict(params)
        limit = int(page_params.get("limit", 100))
        while True:
            page_params["page"] = page
            payload = self._request(path, page_params)
            results = payload.get("results") or []
            for result in results:
                yield result
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            if len(results) < limit:
                return
            page += 1

    def list_locations(
        self,
        *,
        bbox: str,
        parameters_id: list[int],
        limit: int = 1000,
        max_locations: int | None = None,
    ) -> list[dict[str, Any]]:
        """List monitoring locations in `bbox`, filtered by parameter.

        Args:
            bbox: Comma-joined `"west,south,east,north"` WGS84 box.
            parameters_id: OpenAQ numeric parameter ids to filter on.
            limit: Page size.
            max_locations: Cap on the number of locations returned.

        Returns:
            list[dict[str, Any]]: The location objects (each carrying a
                `sensors` list and `coordinates`).
        """
        params: dict[str, Any] = {"bbox": bbox, "limit": limit}
        if parameters_id:
            params["parameters_id"] = parameters_id
        return list(self.paginate("locations", params, max_items=max_locations))

    def list_measurements(
        self,
        *,
        sensor_id: str,
        datetime_from: str,
        datetime_to: str,
        rollup: str | None = None,
        limit: int = 1000,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """List one sensor's measurements over a window (raw or rolled-up).

        The date-window filter parameter depends on the endpoint's
        granularity: the raw `/measurements` and `/hours` rollup take
        `datetime_from`/`datetime_to` (full ISO datetimes), while the
        `/days`, `/months`, and `/years` rollups take `date_from`/
        `date_to` (calendar dates) and silently ignore a `datetime_*`
        filter. This method routes to the correct one
        (see :data:`_DATE_FILTER_ROLLUPS`).

        Args:
            sensor_id: The OpenAQ sensor id.
            datetime_from: Inclusive ISO start of the window. Truncated
                to its `YYYY-MM-DD` date for the date-granularity
                rollups.
            datetime_to: Inclusive ISO end of the window.
            rollup: Server-side rollup segment (`"hours"`, `"days"`,
                `"months"`, `"years"`), or `None` for raw measurements.
            limit: Page size.
            max_items: Optional cap on rows returned.

        Returns:
            list[dict[str, Any]]: The measurement objects for the
                sensor.
        """
        suffix = rollup if rollup else "measurements"
        path = f"sensors/{sensor_id}/{suffix}"
        params: dict[str, Any] = {"limit": limit}
        if rollup in _DATE_FILTER_ROLLUPS:
            params["date_from"] = datetime_from[:10]
            params["date_to"] = datetime_to[:10]
        else:
            params["datetime_from"] = datetime_from
            params["datetime_to"] = datetime_to
        return list(self.paginate(path, params, max_items=max_items))
