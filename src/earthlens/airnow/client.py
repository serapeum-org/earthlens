"""Thin `requests`-based HTTP client for the AirNow `/aq/data/` API.

Owns the two concerns the backend needs: the `API_KEY` query argument
and `429 Too Many Requests` handling with `Retry-After`-aware
exponential back-off. The AirNow `/aq/data/` bounding-box endpoint
returns every matching monitor observation in one JSON array (no
pagination envelope), so unlike `earthlens.openaq.client` there is no
page loop — just one authenticated GET with retry.

This is the local back-off substrate the plan's `G7` decision settled
on: the shared `earthlens.base.http.HttpClient` (the planned foundation
task) does not exist yet, so the client owns its own retry loop,
mirroring `earthlens.openaq.client`. If that primitive lands later, this
client can be re-pointed at it without changing the backend.

`requests` is already a core earthlens dependency, so this client adds
none. The session is injectable (`session=`) so tests drive it with a
fake transport — no live network and no real sleeps (pass `sleep=` to
capture back-off waits).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests
from loguru import logger

#: AirNow bounding-box observations endpoint.
BASE_URL = "https://www.airnowapi.org/aq/data/"


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    AirNow returns `Retry-After` as an integer number of seconds. A
    missing or non-numeric value yields `None` so the caller falls back
    to exponential back-off.

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable.

    Examples:
        - A numeric value parses to seconds; junk yields `None`:
            ```python
            >>> from earthlens.airnow.client import _parse_retry_after
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


class AirnowClient:
    """Minimal AirNow `/aq/data/` client: auth argument + back-off.

    Wraps a `requests.Session`, attaching the `API_KEY` query argument to
    every request and retrying on `429` with a `Retry-After`-aware
    exponential back-off. Exposes one method, `get_data`, that returns the
    endpoint's JSON array of monitor observations.

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
            api_key: The AirNow `API_KEY` to attach to every request.
            session: An existing `requests.Session` to reuse. Defaults to
                a fresh session. Injectable so tests can supply a fake
                transport.
            max_retries: Maximum `429` retries before raising.
            backoff_factor: Base seconds for exponential back-off when the
                response carries no `Retry-After` header.
            timeout: Per-request timeout in seconds.
            sleep: The sleep function used between retries. Defaults to
                `time.sleep`; injectable so tests run without real delays.
        """
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._sleep = sleep

    def get_data(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """GET the observations array for `params`, retrying on `429`.

        Honours a `Retry-After` header when present, otherwise backs off
        exponentially (`backoff_factor * 2**attempt`). After `max_retries`
        exhausted `429`s, the final response's `raise_for_status`
        propagates. Any non-`429` HTTP error raises immediately. The
        `API_KEY` and a JSON `format` are added to `params` here so the
        caller never has to.

        Args:
            params: Query parameters (`BBOX`, `parameters`, `startDate`,
                `endDate`, `dataType`, `monitorType`, `verbose`,
                `includerawconcentrations`). `API_KEY` and `format` are
                injected by this method.

        Returns:
            list[dict[str, Any]]: The parsed JSON array of monitor
                observation rows (empty list when nothing matched).

        Raises:
            requests.HTTPError: On a non-`429` error status, or after
                `max_retries` exhausted `429` responses.
        """
        query = dict(params)
        query["API_KEY"] = self._api_key
        query["format"] = "application/json"
        attempt = 0
        while True:
            response = self._session.get(
                BASE_URL, params=query, timeout=self.timeout
            )
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after
                    if retry_after is not None
                    else self.backoff_factor * (2**attempt)
                )
                logger.warning(
                    "AirNow rate-limited (429); retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
