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


def files_for_year(records: list[dict[str, Any]], year: int | None) -> list[str]:
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
        record = next((d for d in records if int(d["popyear"]) == int(year)), None)
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


def global_records(
    alias: str,
    subalias_id: str,
    *,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
    timeout: int = _TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the index records for a global / non-ISO3 sub-alias.

    Global mosaics (`wpgp1km`, `aswpgponekm`, …) are queried **without**
    `iso3`. The listing carries one summary record per `popyear` (`id`,
    `popyear`, `title`) but **no** `files` — fetch those per record via
    `record_files`.

    Args:
        alias: Top-level product alias (`"pop"`, `"age_structures"`).
        subalias_id: A global-scope sub-alias id (`"wpgp1km"`).
        base_url: REST base URL (overridable for tests).
        session: Optional `requests.Session` to reuse a connection.
        timeout: Per-request timeout in seconds.

    Returns:
        list[dict]: The `data` array — one summary record per year.

    Raises:
        requests.HTTPError: If the endpoint returns a non-2xx status.
    """
    getter = session.get if session is not None else requests.get
    resp = getter(f"{base_url}/{alias}/{subalias_id}", timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("data", [])


def record_files(
    alias: str,
    subalias_id: str,
    record_id: str,
    *,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
    timeout: int = _TIMEOUT,
) -> list[str]:
    """Return the GeoTIFF URLs of one record via the `?id=` detail endpoint.

    The summary listing omits `files`; querying `?id={record_id}` returns the
    full record (a dict) whose `files` array holds the download URLs.

    Args:
        alias: Top-level product alias.
        subalias_id: The sub-alias id.
        record_id: The `id` of a record from `global_records`.
        base_url: REST base URL (overridable for tests).
        session: Optional `requests.Session`.
        timeout: Per-request timeout in seconds.

    Returns:
        list[str]: The record's GeoTIFF URLs (non-raster entries — e.g. the
            `.zip` / `.7z` archives the projection / continent products ship
            — are dropped, so this is empty for archive-only products).

    Raises:
        requests.HTTPError: If the endpoint returns a non-2xx status.
    """
    getter = session.get if session is not None else requests.get
    resp = getter(
        f"{base_url}/{alias}/{subalias_id}", params={"id": record_id}, timeout=timeout
    )
    resp.raise_for_status()
    data = resp.json().get("data")
    record = data[0] if isinstance(data, list) and data else (data or {})
    return [
        url
        for url in (record.get("files") or [])
        if url.lower().endswith((".tif", ".tiff"))
    ]


def global_files_for_year(
    alias: str,
    subalias_id: str,
    year: int | None,
    *,
    base_url: str = BASE_URL,
    session: requests.Session | None = None,
    timeout: int = _TIMEOUT,
) -> list[str]:
    """Return the global GeoTIFF URLs for one `(alias, subalias, year)`.

    Lists the global records, picks the one whose `popyear` matches `year`
    (or the latest when `year` is `None`), then resolves its `files` via the
    `?id=` detail endpoint.

    Args:
        alias: Top-level product alias.
        subalias_id: A global-scope sub-alias id.
        year: The wanted year, or `None` for the latest.
        base_url: REST base URL (overridable for tests).
        session: Optional `requests.Session`.
        timeout: Per-request timeout in seconds.

    Returns:
        list[str]: The matching record's GeoTIFF URLs (one global mosaic for
            plain population; one per age/sex cohort for `age_structures`).

    Raises:
        ValueError: If no records exist, the year is unavailable, or the
            matched record carries no GeoTIFF (e.g. an archive-only product).
    """
    records = global_records(
        alias, subalias_id, base_url=base_url, session=session, timeout=timeout
    )
    dated = [r for r in records if r.get("popyear") is not None]
    if not dated:
        raise ValueError(
            f"WorldPop {alias}/{subalias_id} has no dated global records "
            "(archive-distributed product?)."
        )
    if year is None:
        record = max(dated, key=lambda d: int(d["popyear"]))
    else:
        record = next(
            (d for d in dated if int(d["popyear"]) == int(year)), None
        )
        if record is None:
            available = sorted({int(d["popyear"]) for d in dated})
            raise ValueError(
                f"WorldPop {alias}/{subalias_id} year {year} is not available; "
                f"have {available}."
            )
    tifs = record_files(
        alias, subalias_id, record["id"], base_url=base_url, session=session,
        timeout=timeout,
    )
    if not tifs:
        raise ValueError(
            f"WorldPop {alias}/{subalias_id} record {record.get('popyear')!r} "
            "carries no GeoTIFF (archive-distributed product?)."
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
