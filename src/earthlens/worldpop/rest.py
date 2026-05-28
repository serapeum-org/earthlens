"""WorldPop REST query layer.

Thin `requests`-based client over `hub.worldpop.org/rest/data`. The hub's
three-level scheme is: `/rest/data` (product aliases) →
`/rest/data/{alias}` (sub-aliases) → `/rest/data/{alias}/{subalias}?iso3=…`
(one JSON record per year, each carrying a `files` array of GeoTIFF URLs).
Year filtering is **client-side on `popyear`** — the query returns every
year for the `(alias, subalias, iso3)` triple, so the caller picks the
record whose `popyear` matches.
"""

from __future__ import annotations

from typing import Any

import requests

#: Base URL of the WorldPop REST data catalogue (no auth; CC-BY-4.0).
BASE_URL: str = "https://hub.worldpop.org/rest/data"

#: Default per-request timeout in seconds.
_TIMEOUT: int = 60


def rest_records(
    alias: str,
    subalias_id: str,
    iso3: str,
    *,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
    timeout: int = _TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the WorldPop records for one `(alias, subalias, iso3)` triple.

    The endpoint returns **every year** as one record each; year filtering
    happens client-side in `files_for_year`.

    Args:
        alias: Top-level product alias (`"pop"`, `"age_structures"`, …).
        subalias_id: The concrete REST sub-alias id (`"wpgp"`, …).
        iso3: An ISO 3166-1 alpha-3 country code.
        base_url: REST base URL (overridable for tests).
        session: Optional `requests.Session` to reuse a connection.
        timeout: Per-request timeout in seconds.

    Returns:
        list[dict]: The `data` array — one record per `popyear`, each with
            `id`, `title`, `doi`, `date`, `popyear`, `citation`,
            `license`, and `files` (a list of GeoTIFF URLs).

    Raises:
        requests.HTTPError: If the endpoint returns a non-2xx status.
    """
    getter = session.get if session is not None else requests.get
    resp = getter(
        f"{base_url}/{alias}/{subalias_id}",
        params={"iso3": iso3},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def files_for_year(
    records: list[dict[str, Any]], year: int | None
) -> list[str]:
    """Return the GeoTIFF URLs for the record whose `popyear` matches `year`.

    Args:
        records: The list returned by `rest_records`.
        year: The wanted year. `None` selects the latest available
            `popyear`.

    Returns:
        list[str]: The matching record's GeoTIFF URLs (one for plain
            population products; many — one per age/sex cohort — for
            `age_structures`). Non-raster `files` entries (e.g. the
            `…_ASCII_XYZ.zip` companion the 1 km products ship) are dropped.

    Raises:
        ValueError: If `records` is empty, no record has `popyear == year`,
            or the matching record carries no GeoTIFF; the message lists the
            available years.
    """
    if not records:
        raise ValueError("WorldPop returned no records for this query.")
    if year is None:
        record = max(records, key=lambda d: int(d["popyear"]))
    else:
        record = next(
            (d for d in records if int(d["popyear"]) == int(year)), None
        )
        if record is None:
            available = sorted({int(d["popyear"]) for d in records})
            raise ValueError(
                f"WorldPop year {year} is not available; have {available}."
            )
    tifs = [
        url
        for url in record.get("files", [])
        if url.lower().endswith((".tif", ".tiff"))
    ]
    if not tifs:
        raise ValueError(
            f"WorldPop record {record.get('popyear')!r} carries no GeoTIFF; "
            f"files: {record.get('files', [])}."
        )
    return tifs


def record_citation(records: list[dict[str, Any]]) -> str | None:
    """Return the `citation` of the first record, or `None` if absent.

    Args:
        records: The list returned by `rest_records`.

    Returns:
        str | None: The CC-BY-4.0 citation text WorldPop attaches to the
            dataset, used to stamp output sidecars.
    """
    for record in records:
        citation = record.get("citation")
        if citation:
            return str(citation)
    return None
