"""Thin `requests`-based HTTP client for the OpenAQ v3 API.

Owns the three concerns a pre-v1 SDK would hide: the `X-API-Key`
header, page-by-page iteration of the v3 list endpoints, and — the
backend's main risk — `429 Too Many Requests` handling with
`Retry-After`-aware exponential back-off. OpenAQ's free tier
rate-limits (historically ~60 req/min, ~2000/hour), and the
locations -> sensors -> measurements fan-out makes a continental bbox
hundreds-to-thousands of requests, so back-off is not optional.

The transport — session, `X-API-Key` header, and the `Retry-After`-aware
`429` back-off loop — is delegated to the shared
`earthlens.base.http.HttpClient`; this module keeps only the `API`-shaped
concerns (endpoint paths, the v3 pagination envelope, and the raw-vs-rollup
date-filter routing). The `429`-only retry policy is preserved exactly by
constructing the client with `status_forcelist=(429,)`.

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

from earthlens.base.http import HttpClient

#: OpenAQ v3 API base URL. All endpoint paths are joined onto this.
BASE_URL = "https://api.openaq.org/v3"

#: Maximum page size the v3 list endpoints accept. A `limit` above this
#: is rejected with HTTP 422 (verified live 2026-05-23), so the client
#: clamps to it — both to avoid the 422 and so that "a page shorter than
#: the requested limit is the last page" is a reliable end-of-pages
#: signal (the server never returns more than we ask for).
_MAX_PAGE_SIZE = 1000

#: Rollup endpoints that filter on a calendar `date_from`/`date_to`
#: (date granularity) rather than `datetime_from`/`datetime_to`. The raw
#: `/measurements` and the `/hours` rollup take datetime filters; `/days`,
#: `/months`, and `/years` take date filters and *silently ignore* a
#: `datetime_*` filter (returning the sensor's full history). Verified
#: against the live v3 API on 2026-05-22.
_DATE_FILTER_ROLLUPS = frozenset({"days", "months", "years"})


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
        self._http = HttpClient(
            session=session if session is not None else requests.Session(),
            headers={"X-API-Key": api_key},
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            timeout=timeout,
            status_forcelist=(429,),
            max_backoff=None,
            sleep=sleep,
        )

    @property
    def max_retries(self) -> int:
        """Maximum `429` retries before the last error is raised."""
        return self._http.max_retries

    @property
    def backoff_factor(self) -> float:
        """Base seconds for exponential back-off (no `Retry-After`)."""
        return self._http.backoff_factor

    @property
    def timeout(self) -> float:
        """Per-request timeout in seconds."""
        return self._http.timeout

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
        return self._http.get_json(f"{BASE_URL}/{path}", params=params)

    def paginate(
        self,
        path: str,
        params: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every result across pages of a v3 list endpoint.

        Walks `page=1, 2, ...` until the endpoint is exhausted (or
        `max_items` is reached), yielding each element of the `results`
        array. The requested `limit` is clamped to
        :data:`_MAX_PAGE_SIZE` (the v3 maximum), so the server never
        returns more rows than asked and a page shorter than that limit
        reliably marks the last page — no silent truncation, no extra
        empty round-trip.

        Args:
            path: List-endpoint path relative to :data:`BASE_URL`.
            params: Query parameters; `limit` requests the page size
                (clamped to :data:`_MAX_PAGE_SIZE`).
            max_items: Stop after yielding this many results. `None`
                (default) means no cap — exhaust the endpoint.

        Yields:
            dict[str, Any]: One result object per element.
        """
        page = 1
        yielded = 0
        page_params = dict(params)
        limit = min(int(page_params.get("limit", _MAX_PAGE_SIZE)), _MAX_PAGE_SIZE)
        page_params["limit"] = limit
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
        """List monitoring locations in `bbox`, narrowed by parameter.

        `parameters_id` is a **best-effort server-side narrowing hint**,
        not the source of truth: live probing showed `/locations`
        returning the same locations regardless of the `parameters_id`
        value, and a returned location carries *all* its sensors (not
        just the requested ones). The caller (`OpenAQ._search`) is
        responsible for the authoritative per-sensor filter, which it
        does by parameter **name**.

        Args:
            bbox: Comma-joined `"west,south,east,north"` WGS84 box.
            parameters_id: OpenAQ numeric parameter ids to narrow by
                (best-effort; see above).
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
