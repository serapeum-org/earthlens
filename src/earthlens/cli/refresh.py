"""Live upstream-index refresh — the one *online* CLI operation.

Every other CLI command is strictly offline: it reads only the bundled
catalog YAML. `refresh` is the deliberate exception (the L4 design item):
it makes live HTTP requests to a provider's public API to fetch its
*current* list of datasets / collections, and diffs that against the
bundled `available_datasets` index so the user can see what has appeared
or disappeared upstream.

Only providers with a public, no-auth listing endpoint have a refresher
wired up (currently STAC's `/collections`); every other provider reports
`unsupported` so `refresh all` degrades gracefully instead of failing.
Adding a provider is one entry in :data:`_REFRESHERS`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml

from earthlens.cli.adapter import BackendInfo, load_catalog

#: HTTP timeout (seconds) for a single live-listing request.
_TIMEOUT = 30

#: Cap on STAC `/collections` pages followed via `rel="next"` — a guard
#: against a misbehaving endpoint paginating forever.
_MAX_PAGES = 50


@dataclass
class RefreshOutcome:
    """The result of refreshing one provider against its live index.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"` (live index fetched), `"unsupported"` (no live
            endpoint wired up), or `"error"` (the request failed).
        detail: A human-readable note — the failure reason for `"error"` /
            `"unsupported"`, empty for `"ok"`.
        live_count: Number of distinct ids the live endpoint returned.
        bundled_count: Number of ids in the bundled `available_datasets`.
        new_ids: Ids present live but absent from the bundled index.
        removed_ids: Ids in the bundled index but absent live.
        written: Path of the bundled catalog file rewritten under
            `--write`, or `""` when nothing was written.
    """

    provider: str
    status: str
    detail: str = ""
    live_count: int = 0
    bundled_count: int = 0
    new_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    written: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Project the outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A successful outcome carries the diff counts and id lists:

                ```python
                >>> from earthlens.cli.refresh import RefreshOutcome
                >>> outcome = RefreshOutcome(
                ...     "stac", "ok", live_count=3, bundled_count=2,
                ...     new_ids=["c"], removed_ids=[],
                ... )
                >>> outcome.to_dict()["status"]
                'ok'
                >>> outcome.to_dict()["new_ids"]
                ['c']

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "live_count": self.live_count,
            "bundled_count": self.bundled_count,
            "new_ids": self.new_ids,
            "removed_ids": self.removed_ids,
            "written": self.written,
        }


def _get_json(url: str) -> dict[str, Any]:
    """GET `url` and return the parsed JSON body (raising on HTTP error)."""
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _stac_grouped(catalog: Any) -> dict[str, list[str]]:
    """List collection ids per STAC endpoint, live.

    Hits `{endpoint.url}/collections` for each configured endpoint and
    follows `rel="next"` pagination links (bounded by :data:`_MAX_PAGES`).
    The per-endpoint grouping is what `--write` persists back into the
    `available_collections:` block; callers wanting a flat list use
    :func:`_flatten`.

    Args:
        catalog: The loaded STAC `Catalog` (exposes `endpoints`).

    Returns:
        A mapping of endpoint name to its sorted, de-duplicated collection
        ids, in the catalog's endpoint order.
    """
    grouped: dict[str, list[str]] = {}
    for name, endpoint in catalog.endpoints.items():
        ids: set[str] = set()
        url: str | None = endpoint.url.rstrip("/") + "/collections"
        pages = 0
        while url and pages < _MAX_PAGES:
            body = _get_json(url)
            for collection in body.get("collections", []):
                cid = collection.get("id")
                if cid:
                    ids.add(str(cid))
            url = next(
                (
                    link.get("href")
                    for link in body.get("links", [])
                    if link.get("rel") == "next"
                ),
                None,
            )
            pages += 1
        grouped[name] = sorted(ids)
    return grouped


def _write_stac(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite STAC's `available_collections:` block from a live fetch.

    Replaces only the `available_collections:` block of the bundled
    `_index.yaml`, preserving the header comments and the `endpoints:`
    block above it verbatim. Meaningful in an editable / source checkout
    (it rewrites the package's catalog file); in an installed wheel it
    rewrites the copy under `site-packages`.

    Args:
        info: The STAC backend.
        grouped: Endpoint-name -> live collection ids (see :func:`_stac_grouped`).

    Returns:
        The path of the file rewritten.

    Raises:
        ValueError: If the index file has no `available_collections:` block.
    """
    module = importlib.import_module(f"{info.module}.catalog")
    index_path = module.CATALOG_PATH / "_index.yaml"
    text = index_path.read_text(encoding="utf-8")
    marker = "\navailable_collections:"
    if marker not in text:
        raise ValueError(f"no available_collections block in {index_path}")
    head = text.split(marker, 1)[0].rstrip("\n")
    block = yaml.safe_dump(
        {"available_collections": grouped},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    index_path.write_text(f"{head}\n\n{block}", encoding="utf-8")
    return str(index_path)


#: CDSE openEO collections endpoint (public; the backend's default host).
_OPENEO_COLLECTIONS_URL = (
    "https://openeo.dataspace.copernicus.eu/openeo/1.2/collections"
)


def _openeo_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the CDSE openEO collection ids, live (public, anonymous).

    Args:
        catalog: The loaded openEO `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"openeo": [sorted collection ids]}`.
    """
    body = _get_json(_OPENEO_COLLECTIONS_URL)
    ids = sorted({str(c["id"]) for c in body.get("collections", []) if c.get("id")})
    return {"openeo": ids}


#: HDX CKAN dataset-name listing (public, anonymous).
_HDX_PACKAGE_LIST_URL = "https://data.humdata.org/api/3/action/package_list"


def _hdx_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every HDX (CKAN) dataset name, live (public, anonymous).

    Args:
        catalog: The loaded HDX `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"hdx": [sorted dataset names]}`.
    """
    body = _get_json(_HDX_PACKAGE_LIST_URL)
    return {"hdx": sorted(str(name) for name in body.get("result", []))}


#: NASA CMR collection search (public, anonymous; UMM-JSON).
_CMR_COLLECTIONS_URL = "https://cmr.earthdata.nasa.gov/search/collections.umm_json"


def _cmr_page(provider: str, search_after: str | None) -> tuple[list[str], str | None]:
    """Fetch one CMR collections page for `provider`.

    Args:
        provider: A CMR provider code (e.g. `"GES_DISC"`).
        search_after: The `CMR-Search-After` cursor from the previous page,
            or `None` for the first page.

    Returns:
        `(short_names, next_search_after)` — the page's collection short
        names and the cursor for the next page (`None` when exhausted).
    """
    headers = {"CMR-Search-After": search_after} if search_after else {}
    response = requests.get(
        _CMR_COLLECTIONS_URL,
        params={"provider": provider, "page_size": 2000},
        headers=headers,
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    names = [
        short
        for item in response.json().get("items", [])
        if (short := item.get("umm", {}).get("ShortName"))
    ]
    return names, response.headers.get("CMR-Search-After")


def _earthdata_grouped(catalog: Any) -> dict[str, list[str]]:
    """List collection short names per CMR provider, live (public, anonymous).

    Walks `CMR-Search-After` pagination for each provider in the catalog's
    registry (bounded by :data:`_MAX_PAGES` pages per provider).

    Args:
        catalog: The loaded Earthdata `Catalog` (exposes `providers`).

    Returns:
        A mapping of CMR provider code to its sorted collection short names.
    """
    grouped: dict[str, list[str]] = {}
    for code in sorted(catalog.providers):
        names: set[str] = set()
        search_after: str | None = None
        for _ in range(_MAX_PAGES):
            page, search_after = _cmr_page(code, search_after)
            names.update(str(name) for name in page)
            if not search_after:
                break
        grouped[code] = sorted(names)
    return grouped


#: Provider id -> a callable taking the loaded catalog and returning its
#: live ids grouped (e.g. per STAC endpoint). Only providers with a public,
#: no-auth listing endpoint appear here.
_REFRESHERS: dict[str, Callable[[Any], dict[str, list[str]]]] = {
    "stac": _stac_grouped,
    "openeo": _openeo_grouped,
    "hdx": _hdx_grouped,
    "earthdata": _earthdata_grouped,
}

#: Provider id -> a callable that persists a grouped live fetch back into
#: the bundled catalog (the `--write` half). A subset of `_REFRESHERS`.
_WRITERS: dict[str, Callable[[BackendInfo, dict[str, list[str]]], str]] = {
    "stac": _write_stac,
}


def _flatten(grouped: dict[str, list[str]]) -> list[str]:
    """Flatten grouped live ids into one sorted, de-duplicated list.

    Args:
        grouped: A mapping of group name to its id list.

    Returns:
        The sorted union of every group's ids.

    Examples:
        - Ids are unioned and de-duplicated across groups:

            ```python
            >>> from earthlens.cli.refresh import _flatten
            >>> _flatten({"a": ["x", "y"], "b": ["y", "z"]})
            ['x', 'y', 'z']

            ```
    """
    return sorted({ident for ids in grouped.values() for ident in ids})


def supported_providers() -> list[str]:
    """Return the provider ids that have a live refresher wired up.

    Returns:
        The sorted provider ids `refresh` can fetch live.

    Examples:
        - STAC is wired up:

            ```python
            >>> from earthlens.cli.refresh import supported_providers
            >>> "stac" in supported_providers()
            True

            ```
    """
    return sorted(_REFRESHERS)


def _diff(
    live: list[str], bundled: Iterable[str]
) -> tuple[int, int, list[str], list[str]]:
    """Compare a live id list to the bundled index.

    Args:
        live: Ids returned by the live endpoint.
        bundled: Ids from the bundled `available_datasets`.

    Returns:
        `(live_count, bundled_count, new_ids, removed_ids)` where `new_ids`
        are live-only and `removed_ids` are bundled-only, both sorted.

    Examples:
        - One new id appears upstream and one has disappeared:

            ```python
            >>> from earthlens.cli.refresh import _diff
            >>> _diff(["a", "b", "c"], ["a", "b", "x"])
            (3, 3, ['c'], ['x'])

            ```
    """
    live_set = set(live)
    bundled_set = {str(item) for item in bundled}
    return (
        len(live_set),
        len(bundled_set),
        sorted(live_set - bundled_set),
        sorted(bundled_set - live_set),
    )


@dataclass
class AuditOutcome:
    """The result of auditing one provider's curated catalog against live.

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no live endpoint), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        live_count: Number of distinct ids served live.
        curated_count: Number of curated upstream ids checked.
        broken: Curated upstream ids no longer served live (actionable drift).
        untracked: Live ids absent from the bundled index (informational).
    """

    provider: str
    status: str
    detail: str = ""
    live_count: int = 0
    curated_count: int = 0
    broken: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Project the audit outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - A drifted dataset shows up under `broken`:

                ```python
                >>> from earthlens.cli.refresh import AuditOutcome
                >>> AuditOutcome("stac", "ok", broken=["gone"]).to_dict()["broken"]
                ['gone']

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "live_count": self.live_count,
            "curated_count": self.curated_count,
            "broken": self.broken,
            "untracked": self.untracked,
        }


def _curated_collection_ids(catalog: Any) -> list[str]:
    """Return the upstream `collection_id`s a catalog's records curate.

    Used by `audit` for backends whose curated keys are logical aliases
    (e.g. `sentinel-2-l2a`) distinct from the upstream id the provider
    actually serves (e.g. `SENTINEL2_L2A`), which lives in `collection_id`.
    """
    return sorted(
        {
            cid
            for record in catalog.datasets.values()
            if (cid := getattr(record, "collection_id", None))
        }
    )


def _curated_attr_ids(attr: str) -> Callable[[Any], list[str]]:
    """Build a curated-id resolver that reads `attr` off each record.

    Args:
        attr: The record attribute holding the upstream id (e.g. `"hdx_id"`,
            `"short_name"`).

    Returns:
        A function mapping a catalog to its sorted, de-duplicated upstream ids.
    """

    def resolver(catalog: Any) -> list[str]:
        return sorted(
            {
                value
                for record in catalog.datasets.values()
                if (value := getattr(record, attr, None))
            }
        )

    return resolver


#: Provider id -> a callable returning the upstream ids the catalog curates
#: (for the `audit` drift check). Falls back to the dataset keys otherwise.
_CURATED_IDS: dict[str, Callable[[Any], list[str]]] = {
    "stac": _curated_collection_ids,
    "openeo": _curated_collection_ids,
    "hdx": _curated_attr_ids("hdx_id"),
    "earthdata": _curated_attr_ids("short_name"),
}


def audit_one(info: BackendInfo) -> AuditOutcome:
    """Audit a provider's curated catalog against its live index.

    Flags `broken` curated upstream ids the provider no longer serves (the
    actionable drift a `--strict` CI gate fails on) and, informationally,
    `untracked` live ids missing from the bundled index. Reuses the same
    live refresher as :func:`refresh_one`; providers without one report
    `"unsupported"`, and fetch failures report `"error"` — never raises.

    Args:
        info: The backend to audit.

    Returns:
        The :class:`AuditOutcome` for `info`.
    """
    lister = _REFRESHERS.get(info.provider)
    if lister is None:
        return AuditOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no public live-listing endpoint wired up",
        )
    try:
        catalog = load_catalog(info)
        grouped = lister(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return AuditOutcome(provider=info.provider, status="error", detail=str(exc))

    live = set(_flatten(grouped))
    curated_fn = _CURATED_IDS.get(info.provider)
    curated = set(curated_fn(catalog)) if curated_fn else set(catalog.datasets)
    available = {str(ident) for ident in getattr(catalog, "available_datasets", [])}
    return AuditOutcome(
        provider=info.provider,
        status="ok",
        live_count=len(live),
        curated_count=len(curated),
        broken=sorted(curated - live),
        untracked=sorted(live - available),
    )


def refresh_one(info: BackendInfo, write: bool = False) -> RefreshOutcome:
    """Refresh one provider's live index, diff it, and optionally persist it.

    A provider with no registered refresher returns an `"unsupported"`
    outcome; any error fetching / parsing / writing returns an `"error"`
    outcome — neither raises, so `refresh all` never aborts.

    Args:
        info: The backend to refresh.
        write: When `True`, rewrite the bundled `available_*` index from the
            live fetch (providers without a writer report it in `detail`).

    Returns:
        The :class:`RefreshOutcome` for `info`.
    """
    lister = _REFRESHERS.get(info.provider)
    if lister is None:
        return RefreshOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no public live-listing endpoint wired up",
        )
    try:
        catalog = load_catalog(info)
        grouped = lister(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return RefreshOutcome(provider=info.provider, status="error", detail=str(exc))

    live = _flatten(grouped)
    bundled = getattr(catalog, "available_datasets", [])
    live_count, bundled_count, new_ids, removed_ids = _diff(live, bundled)

    written = ""
    detail = ""
    if write:
        writer = _WRITERS.get(info.provider)
        if writer is None:
            detail = "live read only; --write is not supported for this provider"
        else:
            try:
                written = writer(info, grouped)
            except Exception as exc:  # noqa: BLE001 — write failures are reported
                return RefreshOutcome(
                    provider=info.provider,
                    status="error",
                    detail=f"write failed: {exc}",
                    live_count=live_count,
                    bundled_count=bundled_count,
                    new_ids=new_ids,
                    removed_ids=removed_ids,
                )

    return RefreshOutcome(
        provider=info.provider,
        status="ok",
        detail=detail,
        live_count=live_count,
        bundled_count=bundled_count,
        new_ids=new_ids,
        removed_ids=removed_ids,
        written=written,
    )
