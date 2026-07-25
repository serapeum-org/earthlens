"""Thin direct client for the IUCN Red List v4 REST API.

No mature Python client exists for the v4 API, so the IUCN backend talks to
`https://api.iucnredlist.org/api/v4` directly with `requests` (a core
dependency — no extra). v4 authenticates with an `Authorization: Bearer
<token>` header (the retired v3 `apiv3.iucnredlist.org` `?token=` query
param is gone) and advises a ~2-second delay between calls.

Two fetches return a list of assessment row dicts (the backend turns them
into a `pandas.DataFrame`):

* :func:`fetch_species` is the **two-step** species path: it looks a binomial
  up via `GET /taxa/scientific_name?genus_name=&species_name=` (which returns
  the assessment list), then fetches `GET /assessment/{id}` for the latest
  assessment to enrich it with the criteria / population trend, handling the
  flat `red_list_category_code` (list) vs nested `red_list_category.code`
  (detail) shapes.
* :func:`fetch_country` lists a country's assessments via
  `GET /countries/{code}` (ISO alpha-2).

The exact assessment-body field names are verified against the rOpenSci
`rredlist` client, not the OpenAPI schema (whose response bodies are empty
stubs), so the row builders read each field defensively.
"""

from __future__ import annotations

import threading
import time
from typing import Any, cast

import requests
from loguru import logger

from earthlens.biodiversity import parse_retry_after
from earthlens.iucn.auth import AuthenticationError

#: Base URL of the IUCN Red List v4 API (v3 is retired).
BASE_URL = "https://api.iucnredlist.org/api/v4"

#: IUCN advises ~2 s **between** calls. The first call in a session is not
#: delayed; subsequent calls within this window wait the remainder.
THROTTLE_SECONDS = 2.0

#: Module-level monotonic time of the last successful `_get` request, and a
#: companion "has any call happened yet" flag. The flag is what gates the first
#: call — a `0.0` value alone is ambiguous under a simulated (test) clock that
#: starts at zero. Tests reset both via :func:`clear_throttle_state`. The lock
#: guards reads/writes so concurrent callers (e.g. a `ThreadPoolExecutor` of
#: per-species lookups) cannot race past the inter-call window.
_LAST_CALL_MONOTONIC: float = 0.0
_CALLED_ONCE: bool = False
_THROTTLE_LOCK = threading.Lock()

#: Retry policy for transient upstream failures (5xx / 429 / connection errors).
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.0

#: HTTP statuses considered transient and worth retrying.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def clear_throttle_state() -> None:
    """Reset the inter-call throttle (used by tests to isolate runs)."""
    global _LAST_CALL_MONOTONIC, _CALLED_ONCE
    _LAST_CALL_MONOTONIC = 0.0
    _CALLED_ONCE = False


#: Backward-compatible alias for the shared helper. External tests imported
#: this name; re-export so a name change is not a public-surface break.
_parse_retry_after = parse_retry_after


#: Ordered assessment attribute columns and their pandas dtypes.
IUCN_COLUMNS: dict[str, str] = {
    "scientific_name": "string",
    "assessment_id": "Int64",
    "category": "string",
    "criteria": "string",
    "population_trend": "string",
    "year_published": "string",
    "latest": "boolean",
    "possibly_extinct": "boolean",
    "url": "string",
}


def _session(session: requests.Session | None) -> requests.Session:
    """Return the given session or a fresh `requests.Session`.

    Args:
        session: An existing session, or `None` to build one.

    Returns:
        requests.Session: The session to issue requests on.
    """
    return session if session is not None else requests.Session()


def _throttle() -> None:
    """Wait so that consecutive calls are at least `THROTTLE_SECONDS` apart.

    The first call in a session does not wait; subsequent calls sleep only the
    remaining portion of the window. Tests patch `time.sleep`, making this
    instant under the fake clock. Thread-safe — a lock guards the shared
    state so that concurrent callers serialize through the throttle and the
    inter-call window is honoured against the same upstream API.
    """
    global _LAST_CALL_MONOTONIC, _CALLED_ONCE
    # `time.sleep` is intentionally held inside the lock. The throttle
    # enforces "at most one call per THROTTLE_SECONDS" against the same
    # upstream API, so the next thread must observe the updated
    # `_LAST_CALL_MONOTONIC` after the sleep finishes — otherwise multiple
    # threads would all see `elapsed < THROTTLE_SECONDS`, all sleep
    # concurrently, and all hit the API simultaneously. Concurrent callers
    # therefore serialize cumulatively (the Nth thread waits ~(N-1) *
    # THROTTLE_SECONDS before its own request), which is exactly what
    # IUCN's rate-limit advisory requires.
    with _THROTTLE_LOCK:
        if _CALLED_ONCE:
            elapsed = time.monotonic() - _LAST_CALL_MONOTONIC
            if elapsed < THROTTLE_SECONDS:
                time.sleep(THROTTLE_SECONDS - elapsed)
        _LAST_CALL_MONOTONIC = time.monotonic()
        _CALLED_ONCE = True


def _get(
    session: requests.Session,
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict:
    """GET a v4 endpoint with the Bearer header and return parsed JSON.

    Throttles consecutive calls to at most one every :data:`THROTTLE_SECONDS`
    (the IUCN advisory; the first call does not wait). Retries on `429`
    (honouring `Retry-After`) and on `500`/`502`/`503`/`504` with capped
    exponential back-off. Maps `401` to :class:`AuthenticationError`, `404`
    to a clear :class:`ValueError` naming the path so a typo'd species /
    country reads as "not found in the IUCN Red List" rather than a raw
    `HTTPError`. The Bearer token is held by the session and never echoed
    by `requests` in error messages.

    Args:
        session: The HTTP session.
        token: The v4 API token (sent as `Authorization: Bearer`).
        path: Endpoint path under :data:`BASE_URL` (e.g.
            `"taxa/scientific_name"`).
        params: Optional query parameters.

    Returns:
        dict: The parsed JSON response body.

    Raises:
        AuthenticationError: On an HTTP 401 (missing/invalid token).
        ValueError: On an HTTP 404 (unknown species / country / endpoint).
        RuntimeError: On any other non-2xx status after retries are exhausted,
            or on a non-recoverable transport error.
    """
    url = f"{BASE_URL}/{path}"
    headers = {"Authorization": f"Bearer {token}"}
    last_status: int | None = None
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            response = session.get(
                url, params=params or {}, headers=headers, timeout=60
            )
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = BACKOFF_FACTOR * (2**attempt)
                logger.warning(
                    f"IUCN transport error on {path!r}: {type(exc).__name__}; "
                    f"retry {attempt + 1}/{MAX_RETRIES} after {wait:.1f}s"
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"IUCN Red List transport error on /{path} ({type(exc).__name__})."
            ) from None
        status = getattr(response, "status_code", None)
        if status == 401:
            raise AuthenticationError(
                "IUCN rejected the token (HTTP 401). Check IUCN_TOKEN / the token= "
                "argument, or sign up at https://api.iucnredlist.org/users/sign_up."
            )
        if status == 404:
            raise ValueError(
                f"IUCN Red List returned 404 for /{path}; check the species "
                "binomial or country code."
            )
        last_status = status
        if status in _RETRY_STATUSES and attempt < MAX_RETRIES:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            wait = (
                retry_after
                if retry_after is not None
                else BACKOFF_FACTOR * (2**attempt)
            )
            logger.warning(
                f"IUCN Red List returned HTTP {status} on {path!r}; "
                f"retry {attempt + 1}/{MAX_RETRIES} after {wait:.1f}s"
            )
            time.sleep(wait)
            continue
        if status is None or status >= 400:
            raise RuntimeError(f"IUCN Red List returned HTTP {status} for /{path}.")
        return cast("dict[Any, Any]", response.json())
    # Defensive: unreachable today (every iteration above returns or raises).
    # Kept so a future edit that breaks the invariant fails loudly instead of
    # silently exiting the loop.
    raise RuntimeError(  # pragma: no cover
        f"IUCN Red List exhausted {MAX_RETRIES} retries on /{path} "
        f"(last status {last_status})."
    )


def _category(assessment: dict) -> str | None:
    """Read the Red List category code from a flat or nested assessment.

    Args:
        assessment: An assessment dict from the list (flat
            `red_list_category_code`) or the detail body (nested
            `red_list_category.code`).

    Returns:
        The category code (e.g. `"EN"`), or `None`.
    """
    nested = assessment.get("red_list_category")
    if isinstance(nested, dict) and nested.get("code"):
        return cast("str | None", nested["code"])
    return cast("str | None", assessment.get("red_list_category_code"))


def _flatten_label(value: Any) -> str | None:
    """Flatten a v4 `{description: {en: ...}, code: ...}` wrapper to a string.

    Several v4 detail fields (`population_trend`, sometimes `criteria`) come
    back as a wrapped object rather than a bare string. Prefer the English
    description; fall back to the code; pass strings through unchanged; flatten
    a list of any of the above to a `"; "`-joined string so future v4 changes
    that wrap a field in a list cannot silently drop the data.

    Args:
        value: A bare string, a `{description, code}` wrapper, a list of either,
            or `None`.

    Returns:
        The flattened label, or `None`.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_flatten_label(v) for v in value if v is not None]
        joined = "; ".join(p for p in parts if p)
        return joined or None
    if isinstance(value, dict):
        description = value.get("description")
        if isinstance(description, dict):
            english = description.get("en")
            if english:
                return cast("str | None", english)
        if isinstance(description, str) and description:
            return description
        code = value.get("code")
        if code is not None:
            return str(code)
    return None


def _row(scientific_name: str | None, assessment: dict) -> dict[str, Any]:
    """Build one assessment row from a summary assessment dict.

    Args:
        scientific_name: The taxon scientific name for the row.
        assessment: An assessment dict from a `taxa` / `countries` list.

    Returns:
        A row dict keyed by :data:`IUCN_COLUMNS` (detail fields left `None`).
    """
    return {
        "scientific_name": scientific_name,
        "assessment_id": assessment.get("assessment_id"),
        "category": _category(assessment),
        "criteria": _flatten_label(assessment.get("criteria")),
        "population_trend": _flatten_label(assessment.get("population_trend")),
        "year_published": assessment.get("year_published"),
        "latest": assessment.get("latest"),
        "possibly_extinct": assessment.get("possibly_extinct"),
        "url": assessment.get("url"),
    }


def fetch_species(
    token: str,
    genus: str,
    species: str,
    *,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch a species' assessments via the two-step v4 flow.

    Looks the binomial up with `taxa/scientific_name`, then enriches the
    latest assessment with its `assessment/{id}` detail (criteria,
    population trend, nested category).

    Args:
        token: The v4 API token.
        genus: Genus name (e.g. `"Panthera"`).
        species: Species epithet (e.g. `"leo"`).
        session: Optional `requests.Session` (injected in tests).

    Returns:
        list[dict]: One row per assessment, the latest enriched with detail.

    Raises:
        AuthenticationError: On an HTTP 401 (missing/invalid token).
        ValueError: On an HTTP 404 (unknown binomial / endpoint).
        RuntimeError: On any other non-2xx status after retries are exhausted,
            or on a non-recoverable transport error.
    """
    http = _session(session)
    summary = _get(
        http,
        token,
        "taxa/scientific_name",
        {"genus_name": genus, "species_name": species},
    )
    taxon = summary.get("taxon") or {}
    scientific_name = taxon.get("scientific_name") or f"{genus} {species}"
    rows: list[dict] = []
    for assessment in summary.get("assessments") or []:
        row = _row(scientific_name, assessment)
        if assessment.get("latest") and assessment.get("assessment_id") is not None:
            detail = _get(http, token, f"assessment/{assessment['assessment_id']}")
            row["category"] = _category(detail) or row["category"]
            row["criteria"] = _flatten_label(detail.get("criteria")) or row["criteria"]
            row["population_trend"] = (
                _flatten_label(detail.get("population_trend"))
                or row["population_trend"]
            )
        rows.append(row)
    return rows


def fetch_country(
    token: str,
    code: str,
    *,
    session: requests.Session | None = None,
) -> list[dict]:
    """Fetch a country's assessments via `countries/{code}` (ISO alpha-2).

    Args:
        token: The v4 API token.
        code: ISO alpha-2 country code (e.g. `"KE"`).
        session: Optional `requests.Session` (injected in tests).

    Returns:
        list[dict]: One row per assessment in the country list.

    Raises:
        AuthenticationError: On an HTTP 401 (missing/invalid token).
        ValueError: On an HTTP 404 (unknown country code).
        RuntimeError: On any other non-2xx status after retries are exhausted,
            or on a non-recoverable transport error.
    """
    http = _session(session)
    body = _get(http, token, f"countries/{code}")
    rows: list[dict] = []
    for assessment in body.get("assessments") or []:
        taxon = assessment.get("taxon") or {}
        rows.append(_row(taxon.get("scientific_name"), assessment))
    return rows
