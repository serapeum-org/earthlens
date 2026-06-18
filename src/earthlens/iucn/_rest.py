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

import time
from typing import Any

import requests

from earthlens.iucn.auth import AuthenticationError

#: Base URL of the IUCN Red List v4 API (v3 is retired).
BASE_URL = "https://api.iucnredlist.org/api/v4"

#: IUCN advises ~2 s between calls; slept before each request (patched in tests).
THROTTLE_SECONDS = 2.0

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


def _get(
    session: requests.Session,
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict:
    """GET a v4 endpoint with the Bearer header and return parsed JSON.

    Sleeps :data:`THROTTLE_SECONDS` before the request (IUCN's advisory) and
    maps an HTTP 401 to :class:`AuthenticationError`.

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
    """
    time.sleep(THROTTLE_SECONDS)
    response = session.get(
        f"{BASE_URL}/{path}",
        params=params or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if getattr(response, "status_code", None) == 401:
        raise AuthenticationError(
            "IUCN rejected the token (HTTP 401). Check IUCN_TOKEN / the token= "
            "argument, or sign up at https://api.iucnredlist.org/users/sign_up."
        )
    response.raise_for_status()
    return response.json()


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
        return nested["code"]
    return assessment.get("red_list_category_code")


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
        "criteria": assessment.get("criteria"),
        "population_trend": assessment.get("population_trend"),
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
        AuthenticationError: On an HTTP 401.
    """
    http = _session(session)
    summary = _get(
        http, token, "taxa/scientific_name", {"genus_name": genus, "species_name": species}
    )
    taxon = summary.get("taxon") or {}
    scientific_name = taxon.get("scientific_name") or f"{genus} {species}"
    rows: list[dict] = []
    for assessment in summary.get("assessments") or []:
        row = _row(scientific_name, assessment)
        if assessment.get("latest") and assessment.get("assessment_id") is not None:
            detail = _get(http, token, f"assessment/{assessment['assessment_id']}")
            row["category"] = _category(detail) or row["category"]
            row["criteria"] = detail.get("criteria") or row["criteria"]
            row["population_trend"] = detail.get("population_trend") or row["population_trend"]
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
        AuthenticationError: On an HTTP 401.
    """
    http = _session(session)
    body = _get(http, token, f"countries/{code}")
    rows: list[dict] = []
    for assessment in body.get("assessments") or []:
        taxon = assessment.get("taxon") or {}
        rows.append(_row(taxon.get("scientific_name"), assessment))
    return rows
