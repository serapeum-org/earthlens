"""Thin `requests`-based HTTP client for the AirNow `/aq/data/` API.

Owns the two concerns the backend needs: the `API_KEY` query argument
and `429 Too Many Requests` handling with `Retry-After`-aware
exponential back-off. The AirNow `/aq/data/` bounding-box endpoint
returns every matching monitor observation in one JSON array (no
pagination envelope), so unlike `earthlens.openaq.client` there is no
page loop — just one authenticated GET with retry.

The transport — session and the `Retry-After`-aware `429` back-off loop
— is delegated to the shared `earthlens.base.http.HttpClient`; this
module keeps only the `API`-shaped concerns (the `/aq/data/` endpoint,
the `API_KEY` + `format` query arguments, and the JSON-array response).
The `429`-only retry policy is preserved exactly by constructing the
client with `status_forcelist=(429,)`.

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

from earthlens.base.http import HttpClient, Timeout

#: AirNow bounding-box observations endpoint.
BASE_URL = "https://www.airnowapi.org/aq/data/"


class AirnowClient:
    """Minimal AirNow `/aq/data/` client: auth argument + back-off.

    Delegates the transport (session, `429`/`Retry-After` back-off) to
    `earthlens.base.http.HttpClient`, keeping only the AirNow-specific
    request shaping. Exposes one method, `get_data`, that returns the
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
        self._http = HttpClient(
            session=session if session is not None else requests.Session(),
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
    def timeout(self) -> Timeout:
        """Per-request timeout in seconds (a float or a `(connect, read)` pair)."""
        return self._http.timeout

    def get_data(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """GET the observations array for `params`, retrying on `429`.

        The `API_KEY` and a JSON `format` are added to `params` here so
        the caller never has to; the retry/back-off is handled by the
        shared `HttpClient`.

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
        payload = self._http.get_json(BASE_URL, params=query)
        return payload if isinstance(payload, list) else []
