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
from loguru import logger

if TYPE_CHECKING:
    from earthlens.base.http import HttpClient

#: Hard cap on ArcGIS pages walked, a backstop against a layer that ignores
#: paging and reports `exceededTransferLimit` forever.
_MAX_ARCGIS_PAGES: int = 1000

#: Hard cap on NFIP pages walked (page_size up to 1000 → up to ~10M rows), a
#: backstop against an endpoint that ignores `$skip` and never empties out.
_MAX_NFIP_PAGES: int = 10000


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
    order_by: str = "id",
) -> tuple[list[dict], int | None]:
    """Page through the OpenFEMA NFIP endpoint and collect every record.

    Issues `$top`/`$skip` requests until a short page is returned (or
    `max_records` is reached), reading the record list from `records_key` in
    each envelope. The **first** request also carries `$inlinecount=allpages`, so
    the total matching count comes back on the same round-trip rather than a
    separate probe. Every page is `$orderby`-sorted on a stable key so
    `$skip`/`$top` deep paging cannot duplicate or miss rows.

    Args:
        client: The transport used for each GET (injectable for tests).
        endpoint: The NFIP v3 endpoint URL.
        records_key: Envelope key holding the record list (`"NfipClaims"`).
        filter_str: An OData `$filter` expression, or `None` for no filter.
        page_size: Records per page (the `$top` value).
        max_records: Optional cap on the total collected; `None` for no cap.
        select: Optional `$select` comma-separated projection.
        order_by: Stable `$orderby` field for consistent paging (default `id`).

    Returns:
        tuple[list[dict], int | None]: All fetched records (server order), and
            the total matching count from `metadata.count` (`None` if the server
            omitted it).
    """
    collected: list[dict] = []
    skip = 0
    total: int | None = None
    counted = False
    prev_page: list | None = None
    for _ in range(_MAX_NFIP_PAGES):
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
        if order_by:
            params["$orderby"] = order_by
        if not counted:
            params["$inlinecount"] = "allpages"
        payload = client.get_json(endpoint, params=params)
        if not counted and isinstance(payload, dict):
            counted = True
            # `or {}` also guards a present-but-null `metadata` (None.get crashes).
            count = (payload.get("metadata") or {}).get("count")
            total = int(count) if count is not None else None
        page = payload.get(records_key, []) if isinstance(payload, dict) else []
        if page and page == prev_page:
            # Server re-sent the previous page for a new $skip — it does not
            # honour paging. Stop before accumulating duplicates (mirrors
            # paginate_arcgis). The `==` short-circuits on the first record, so
            # this is cheap on the happy path (distinct pages differ at [0]).
            logger.warning(
                f"paginate_nfip: {endpoint!r} re-sent the same page for a new "
                "$skip (server does not honour paging); result may be incomplete."
            )
            break
        collected.extend(page)
        prev_page = page
        if len(page) < top:
            break
        skip += len(page)
    else:  # pragma: no cover - pathological server that paginates past the cap
        logger.warning(
            f"paginate_nfip hit the {_MAX_NFIP_PAGES}-page cap for {endpoint!r}; "
            "result may be incomplete — narrow the filter or set max_records."
        )
    return collected, total


def paginate_arcgis(
    client: HttpClient,
    url: str,
    params: dict,
    *,
    page_size: int = 1000,
) -> dict:
    """Page an ArcGIS `query` and merge every feature into one GeoJSON mapping.

    An ArcGIS `MapServer` layer caps a single response at its `maxRecordCount`
    and flags a truncated result with `exceededTransferLimit`. This walks the
    result with `resultOffset` / `resultRecordCount` until the layer stops
    signalling more, so a dense box is not silently truncated.

    Args:
        client: The transport used for each GET.
        url: The layer's `query` endpoint URL.
        params: The base query params (envelope, `f=geojson`, …) — copied per
            page with the offset/count added.
        page_size: Features requested per page (`resultRecordCount`).

    Returns:
        dict: A GeoJSON `FeatureCollection` mapping with every page's features
            concatenated.
    """
    features: list = []
    offset = 0
    prev_page: list | None = None
    for _ in range(_MAX_ARCGIS_PAGES):
        page_params = {**params, "resultOffset": offset, "resultRecordCount": page_size}
        payload = client.get_json(url, params=page_params)
        page = payload.get("features", []) if isinstance(payload, dict) else []
        if page and page == prev_page:
            # The server re-sent the previous page for a new offset — it does not
            # honour paging. Stop now instead of accumulating duplicate features
            # up to the page cap.
            logger.warning(
                f"paginate_arcgis: {url!r} returned an identical page for a new "
                "offset (server does not honour paging); result may be incomplete."
            )
            break
        features.extend(page)
        prev_page = page
        exceeded = (
            bool(payload.get("exceededTransferLimit"))
            if isinstance(payload, dict)
            else False
        )
        # Continue while the layer flags more (`exceededTransferLimit`) OR a full
        # page came back — a page equal to the requested size is a strong
        # "there may be more" signal even when the optional flag is absent from
        # the f=geojson response. Stop on an empty page, or a short page the
        # layer did not flag (the genuine last page). A short-but-flagged page (a
        # `maxRecordCount` below `page_size`) keeps paging.
        if not page or (not exceeded and len(page) < page_size):
            break
        offset += len(page)
    else:  # pragma: no cover - pathological server that paginates forever
        logger.warning(
            f"paginate_arcgis hit the {_MAX_ARCGIS_PAGES}-page cap for {url!r}; "
            "result may be incomplete — narrow the box."
        )
    return {"type": "FeatureCollection", "features": features}


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

    Examples:
        - Map provider fields to friendly names and read a value:
            ```python
            >>> from earthlens.nsi._helpers import records_to_frame
            >>> frame = records_to_frame(
            ...     [{"id": 1, "amountPaidOnBuildingClaim": 100.0}],
            ...     {"claim_id": "id", "paid": "amountPaidOnBuildingClaim"},
            ... )
            >>> list(frame.columns)
            ['claim_id', 'paid']
            >>> float(frame["paid"].iloc[0])
            100.0

            ```
        - No records with a field map yields an empty, schema-only frame:
            ```python
            >>> from earthlens.nsi._helpers import records_to_frame
            >>> frame = records_to_frame([], {"claim_id": "id"})
            >>> list(frame.columns)
            ['claim_id']
            >>> len(frame)
            0

            ```
    """
    frame = pd.DataFrame(records)
    if field_map is None:
        return frame
    inverse = {provider: friendly for friendly, provider in field_map.items()}
    present = [col for col in inverse if col in frame.columns]
    if not present:
        if not frame.empty:
            # Records came back but none of the mapped provider columns are
            # present — upstream schema drift. Do not silently drop the data.
            logger.warning(
                f"NSI: {len(frame)} record(s) fetched but none of the mapped "
                f"fields {sorted(field_map.values())} are present (columns: "
                f"{sorted(frame.columns)}); returning the raw, unmapped frame."
            )
            return frame
        return pd.DataFrame(columns=list(field_map))
    subset = frame[present].rename(columns=inverse)
    return subset.reindex(columns=list(field_map))
