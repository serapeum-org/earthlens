"""Pure query/parse helpers for the NSI backend's tabular source.

The OpenFEMA NFIP v3 endpoint speaks OData: a `$filter` expression plus
`$top`/`$skip` paging over a `{"metadata": ..., "NfipClaims": [...]}` envelope.
These helpers build the filter, page through the result via an injected
:class:`~earthlens.base.http.HttpClient`, and flatten the records into a
:class:`pandas.DataFrame`. No state; the client is passed in so the whole path
is unit-testable with a fake transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from earthlens.base.http import HttpClient


def _odata_literal(value: str) -> str:
    """Quote a string as an OData literal, doubling any embedded quote.

    Args:
        value: The raw string value.

    Returns:
        str: The value wrapped in single quotes, OData-escaped.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def odata_filter(
    state: str | None = None,
    county: str | None = None,
    year: int | None = None,
    flood_event: str | None = None,
) -> str | None:
    """Build an OData `$filter` string for the NFIP claims query.

    Clauses are joined with `and`; string values are quoted, `year` is emitted
    bare (a numeric field).

    Args:
        state: Two-letter state code (`"LA"`) -> `state eq 'LA'`.
        county: 5-digit county FIPS (`"22071"`) -> `countyCode eq '22071'`.
        year: Loss year (`2005`) -> `yearOfLoss eq 2005`.
        flood_event: Named flood event -> `floodEvent eq '...'`.

    Returns:
        str | None: The `$filter` expression, or `None` when no selector is
            given.

    Examples:
        ```python
        >>> from earthlens.nsi._helpers import odata_filter
        >>> odata_filter(county="22071", year=2005)
        "countyCode eq '22071' and yearOfLoss eq 2005"

        ```
    """
    clauses: list[str] = []
    if state:
        clauses.append(f"state eq {_odata_literal(state)}")
    if county:
        clauses.append(f"countyCode eq {_odata_literal(county)}")
    if year is not None:
        clauses.append(f"yearOfLoss eq {int(year)}")
    if flood_event:
        clauses.append(f"floodEvent eq {_odata_literal(flood_event)}")
    return " and ".join(clauses) if clauses else None


def paginate_nfip(
    client: HttpClient,
    endpoint: str,
    records_key: str,
    *,
    filter_str: str | None,
    page_size: int,
    max_records: int | None = None,
    select: str | None = None,
) -> list[dict]:
    """Page through the OpenFEMA NFIP endpoint and collect every record.

    Issues `$top`/`$skip` requests until a short page is returned (or
    `max_records` is reached), reading the record list from `records_key` in
    each envelope.

    Args:
        client: The transport used for each GET (injectable for tests).
        endpoint: The NFIP v3 endpoint URL.
        records_key: Envelope key holding the record list (`"NfipClaims"`).
        filter_str: An OData `$filter` expression, or `None` for no filter.
        page_size: Records per page (the `$top` value).
        max_records: Optional cap on the total collected; `None` for no cap.
        select: Optional `$select` comma-separated projection.

    Returns:
        list[dict]: All fetched records, in server order.
    """
    collected: list[dict] = []
    skip = 0
    while True:
        top = page_size
        if max_records is not None:
            remaining = max_records - len(collected)
            if remaining <= 0:
                break
            top = min(page_size, remaining)
        params: dict[str, object] = {"$top": top, "$skip": skip}
        if filter_str:
            params["$filter"] = filter_str
        if select:
            params["$select"] = select
        payload = client.get_json(endpoint, params=params)
        page = payload.get(records_key, []) if isinstance(payload, dict) else []
        collected.extend(page)
        if len(page) < top:
            break
        skip += len(page)
    return collected


def nfip_count(
    client: HttpClient,
    endpoint: str,
    *,
    filter_str: str | None,
) -> int:
    """Return the total NFIP record count for a filter via `$inlinecount`.

    Args:
        client: The transport used for the GET.
        endpoint: The NFIP v3 endpoint URL.
        filter_str: An OData `$filter` expression, or `None`.

    Returns:
        int: `metadata.count` for the query (total matching records).
    """
    params: dict[str, object] = {"$inlinecount": "allpages", "$top": 1}
    if filter_str:
        params["$filter"] = filter_str
    payload = client.get_json(endpoint, params=params)
    meta = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    return int(meta.get("count", 0) or 0)


def records_to_frame(
    records: list[dict], field_map: dict[str, str] | None = None
) -> pd.DataFrame:
    """Flatten NFIP records into a :class:`pandas.DataFrame`.

    Args:
        records: The list of record dicts from :func:`paginate_nfip`.
        field_map: Optional friendly -> provider map; when given, only these
            columns are kept and renamed friendly, in map order.

    Returns:
        pd.DataFrame: One row per record. When `field_map` is given, the frame
            has the friendly column names (empty-but-typed when there are no
            records).
    """
    frame = pd.DataFrame(records)
    if field_map is None:
        return frame
    inverse = {provider: friendly for friendly, provider in field_map.items()}
    present = [col for col in inverse if col in frame.columns]
    if not present:
        return pd.DataFrame(columns=list(field_map))
    subset = frame[present].rename(columns=inverse)
    return subset.reindex(columns=list(field_map))
