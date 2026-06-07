"""Live upstream-index refresh — the one *online* CLI operation.

Every other CLI command is strictly offline: it reads only the bundled
catalog YAML. `refresh` is the deliberate exception (the L4 design item):
it makes live HTTP requests to a provider's public API to fetch its
*current* list of datasets / collections, and diffs that against the
bundled `available_datasets` index so the user can see what has appeared
or disappeared upstream.

Only providers with a public listing endpoint (or public SDK call, or
anonymous FTP tree) have a refresher wired up in :data:`_REFRESHERS`; every
other provider reports `unsupported` so `refresh all` degrades gracefully
instead of failing. The live ids are diffed against the bundled index that
fits the provider — usually `available_datasets`, but a backend whose
refresh axis differs (Overture's `available_releases`, CHC's `ftp_bases`
paths, or radar / firms / fdsn whose `datasets` map *is* the index) resolves
its own via :func:`_bundled_ids`.

The `--write` half (:data:`_WRITERS`) persists a live fetch back into the
bundled informational index. For the sharded `_index.yaml` providers (and
HDX's gzipped sidecar) it rewrites the in-file block; for the providers
whose `available_*` attribute is *computed* from the curated rows at load
time (openaq, worldpop, usgs_water) it instead writes the full live universe
to a sibling `available_*.yaml` the runtime does not load (the maintainer /
docs artefact the tools used to produce). Only the few backends with no
machine-writable index at all (chc's curated slugs, fdsn / firms whose
`datasets` map *is* the catalog) stay read-only under `--write`.
"""

from __future__ import annotations

import gzip
import importlib
import json
import os
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from ftplib import FTP, error_perm  # nosec B402  # noqa: S402
from pathlib import Path
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


def _get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET `url` and return the parsed JSON body (raising on HTTP error).

    Args:
        url: The endpoint to fetch.
        headers: Optional request headers (e.g. an `X-API-Key` for a
            credentialed provider).
        params: Optional query parameters.

    Returns:
        The parsed JSON body.
    """
    response = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
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


def _index_path(info: BackendInfo) -> Path:
    """Return the bundled index file a provider's `--write` rewrites.

    Resolves the catalog's `CATALOG_PATH`: a sharded layout (a directory)
    keeps its informational index in `_index.yaml`; a single-file layout
    *is* the catalog file itself.

    Args:
        info: The backend whose catalog path to resolve.

    Returns:
        The `_index.yaml` under a sharded `catalog/` directory, or the
        single `<pkg>_data_catalog.yaml` file.
    """
    base = importlib.import_module(f"{info.module}.catalog").CATALOG_PATH
    return base / "_index.yaml" if base.is_dir() else base


def _replace_index_block(path: Path, block_key: str, payload: Any) -> None:
    """Replace exactly one top-level block of a YAML index in place.

    Rewrites the `{block_key}:` block (from its key line up to the next
    column-zero key, or end of file) with `payload`, leaving every other
    block — and the header comments above it — byte-for-byte intact. This
    is what lets a provider whose `_index.yaml` holds more than one block
    (e.g. openEO's `available_collections:` *and* `available_processes:`)
    be rewritten without disturbing the sibling block.

    Args:
        path: The YAML index file to rewrite.
        block_key: The top-level key whose block is replaced.
        payload: The new value for `block_key` (a flat list, or a grouped
            mapping for backends that persist their index grouped).

    Raises:
        ValueError: If `path` has no `{block_key}:` block.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{block_key}:")),
        None,
    )
    if start is None:
        raise ValueError(f"no {block_key}: block in {path}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*:", lines[j]):
            end = j
            break
    block = yaml.safe_dump(
        {block_key: payload},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    path.write_text("".join(lines[:start]) + block + "".join(lines[end:]), "utf-8")


def _index_writer(
    block_key: str, *, grouped: bool = False
) -> Callable[[BackendInfo, dict[str, list[str]]], str]:
    """Build a writer that persists a live fetch into a YAML index block.

    The returned writer flattens the grouped live fetch (or keeps it
    grouped, for backends that persist their index per-group) and splices
    it into the provider's `_index.yaml` via :func:`_replace_index_block`.

    Args:
        block_key: The index block to rewrite (`"available_datasets"` or
            `"available_collections"`).
        grouped: When `True`, persist the per-group mapping verbatim;
            when `False`, persist the flat sorted union.

    Returns:
        A `(info, grouped_ids) -> written_path` writer for `_WRITERS`.
    """

    def writer(info: BackendInfo, grouped_ids: dict[str, list[str]]) -> str:
        path = _index_path(info)
        payload = grouped_ids if grouped else _flatten(grouped_ids)
        _replace_index_block(path, block_key, payload)
        return str(path)

    return writer


def _write_hdx(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite HDX's gzipped `_available.json.gz` index, merge-preserving.

    The live CKAN `package_list` returns only dataset *names*, whereas the
    sidecar carries an `{org, title}` row per name. To avoid discarding
    that metadata, surviving names keep their existing row and only
    genuinely-new names get a bare row; names gone upstream are dropped.
    (Full `org`/`title` enrichment for new names still belongs to
    `tools/hdx/refresh_hdx_catalog.py`, which fetches `package_show`.)

    Args:
        info: The HDX backend.
        grouped: Group name -> live dataset names (see :func:`_hdx_grouped`).

    Returns:
        The path of the sidecar rewritten.
    """
    path = _index_path(info).parent / "_available.json.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            previous = json.load(handle).get("datasets", {})
    except FileNotFoundError:
        previous = {}
    datasets = {
        name: previous.get(name, {"org": "", "title": ""}) for name in _flatten(grouped)
    }
    payload = {
        "__comment__": "AUTO-GENERATED by `earthlens datasets refresh hdx --write`. "
        "Every HDX dataset id with its org/title; new ids carry empty "
        "org/title until enriched by tools/hdx/refresh_hdx_catalog.py.",
        "datasets": datasets,
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    return str(path)


#: Copernicus CDS public STAC catalogue (collection listing needs no auth).
_CDS_COLLECTIONS_URL = "https://cds.climate.copernicus.eu/api/catalogue/v1/collections"


def _ecmwf_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the Copernicus CDS dataset ids, live (public catalogue).

    Listing the CDS catalogue needs no credentials (only data *retrieval*
    does); each collection's `id` is a CDS dataset name.

    Args:
        catalog: The loaded ECMWF `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"ecmwf": [sorted CDS dataset ids]}`.
    """
    body = _get_json(_CDS_COLLECTIONS_URL)
    ids = sorted({str(c["id"]) for c in body.get("collections", []) if c.get("id")})
    return {"ecmwf": ids}


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


#: CDSE openEO processes endpoint (public; pairs with the collections one).
_OPENEO_PROCESSES_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/processes"


def _openeo_process_ids() -> list[str]:
    """List the live CDSE openEO process ids (public, anonymous)."""
    body = _get_json(_OPENEO_PROCESSES_URL)
    return sorted({str(p["id"]) for p in body.get("processes", []) if p.get("id")})


def _write_openeo(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite openEO's `available_collections` AND `available_processes`.

    The collection index comes from the live fetch (`grouped`); the process
    index is fetched separately — so `--write` keeps both informational
    blocks of `_index.yaml` current (the generic writer only does one).

    Args:
        info: The openEO backend.
        grouped: Group name -> live collection ids (see :func:`_openeo_grouped`).

    Returns:
        The path of the rewritten `_index.yaml`.
    """
    path = _index_path(info)
    _replace_index_block(path, "available_collections", _flatten(grouped))
    _replace_index_block(path, "available_processes", _openeo_process_ids())
    return str(path)


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


#: OpenAQ parameters endpoint (needs an `OPENAQ_API_KEY` header).
_OPENAQ_PARAMETERS_URL = "https://api.openaq.org/v3/parameters"


def _openaq_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the OpenAQ parameter names, live (needs `OPENAQ_API_KEY`).

    The key is read from the environment; without it the request fails and
    `refresh_one` reports an `"error"` outcome.

    Args:
        catalog: The loaded OpenAQ `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"openaq": [sorted parameter names]}`.
    """
    key = os.environ.get("OPENAQ_API_KEY", "")
    body = _get_json(
        _OPENAQ_PARAMETERS_URL,
        headers={"X-API-Key": key} if key else None,
        params={"limit": 1000},
    )
    names = sorted(
        {str(row["name"]) for row in body.get("results", []) if row.get("name")}
    )
    return {"openaq": names}


def _cmems_describe() -> Any:
    """Return the live Copernicus Marine catalogue (SDK call, public)."""
    import copernicusmarine

    return copernicusmarine.describe(disable_progress_bar=True)


def _cmems_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every CMEMS dataset id across the live catalogue (public SDK).

    Walks `copernicusmarine.describe().products[].datasets[].dataset_id`.

    Args:
        catalog: The loaded CMEMS `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"cmems": [sorted dataset ids]}`.
    """
    result = _cmems_describe()
    ids = {
        did
        for product in getattr(result, "products", []) or []
        for dataset in getattr(product, "datasets", []) or []
        if (did := getattr(dataset, "dataset_id", None))
    }
    return {"cmems": sorted(str(i) for i in ids)}


#: EUMETSAT public browse collections endpoint (no credentials).
_EUMETSAT_BROWSE_URL = "https://api.eumetsat.int/data/browse/collections"


def _eumetsat_grouped(catalog: Any) -> dict[str, list[str]]:
    """List EUMETSAT collection ids from the public browse endpoint.

    Listing collections needs no credentials (only data *access* does); each
    browse link's `title` is the `EO:EUM:...` collection id.

    Args:
        catalog: The loaded EUMETSAT `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"eumetsat": [sorted collection ids]}`.
    """
    body = _get_json(_EUMETSAT_BROWSE_URL, params={"format": "json"})
    ids = sorted(
        {str(link["title"]) for link in body.get("links", []) if link.get("title")}
    )
    return {"eumetsat": ids}


def _sh_data_collection_names() -> list[str]:
    """Return the `sentinelhub.DataCollection` enum member names (no auth)."""
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    return [member.name for member in import_sentinelhub().DataCollection]


def _sentinel_hub_grouped(catalog: Any) -> dict[str, list[str]]:
    """List Sentinel Hub collections from the SDK's `DataCollection` enum.

    Listing the supported collections needs no credentials — it is the
    sentinelhub SDK's authoritative registry (the same source the bundled
    `available_collections` index was built from); data *access* is what
    needs CDSE OAuth.

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (unused; the SDK is the
            source).

    Returns:
        A single-group mapping `{"sentinel_hub": [sorted collection names]}`.
    """
    return {"sentinel_hub": sorted(set(_sh_data_collection_names()))}


#: Public Earth Engine STAC catalog root (no credentials for the catalog).
_GEE_STAC_ROOT = "https://storage.googleapis.com/earthengine-stac/catalog/catalog.json"


def _gee_dataset_hrefs() -> list[str]:
    """Walk the public EE STAC tree and return every dataset STAC-doc href.

    BFS over `rel="child"` links from the root (absolute hrefs); links to
    `…/catalog.json` are sub-catalogs to recurse, the rest are dataset docs.

    Returns:
        The dataset STAC-document hrefs (one per Earth Engine asset).
    """
    hrefs: list[str] = []
    queue = [_GEE_STAC_ROOT]
    seen: set[str] = set()
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        try:
            node = _get_json(url)
        except Exception:  # noqa: BLE001 — skip an unreachable sub-catalog
            continue
        for link in node.get("links", []):
            if link.get("rel") != "child":
                continue
            href = link.get("href")
            if not href:
                continue
            (queue if href.endswith("/catalog.json") else hrefs).append(href)
    return hrefs


def _gee_fetch_id(href: str) -> str | None:
    """Return a dataset STAC doc's `id` (its EE asset id), or None on error."""
    try:
        return _get_json(href).get("id")
    except Exception:  # noqa: BLE001 — a single unreachable doc is skipped
        return None


def _gee_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every Earth Engine asset id from the public STAC catalog.

    Walks the STAC tree for dataset docs, then fetches each doc's `id`
    concurrently (pure HTTP, no SDK / credentials).

    Args:
        catalog: The loaded GEE `Catalog` (unused; the STAC tree is the source).

    Returns:
        A single-group mapping `{"gee": [sorted asset ids]}`.
    """
    hrefs = _gee_dataset_hrefs()
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = {str(cid) for cid in pool.map(_gee_fetch_id, hrefs) if cid}
    return {"gee": sorted(ids)}


#: WorldPop public REST data hub (alias -> sub-alias crawl, no credentials).
_WORLDPOP_REST_URL = "https://hub.worldpop.org/rest/data"


def _worldpop_grouped(catalog: Any) -> dict[str, list[str]]:
    """List WorldPop sub-alias ids per product alias, live (public REST).

    Fetches the top-level alias list, then each alias's sub-alias rows; a
    row's `alias` field is the sub-alias id (e.g. `G2_BUILT_S`, `wpgp`) —
    the same namespace the curated records and `available_datasets` use.

    Args:
        catalog: The loaded WorldPop `Catalog` (unused; the REST is the source).

    Returns:
        A mapping of product alias to its sorted sub-alias ids.
    """
    top = _get_json(_WORLDPOP_REST_URL).get("data", [])
    grouped: dict[str, list[str]] = {}
    for entry in top:
        alias = entry.get("alias")
        if not alias:
            continue
        rows = _get_json(f"{_WORLDPOP_REST_URL}/{alias}").get("data", [])
        grouped[str(alias)] = sorted(
            {sub for row in rows if (sub := str(row.get("alias", "")).strip())}
        )
    return grouped


def _worldpop_curated_ids(catalog: Any) -> list[str]:
    """Return the sub-alias ids the WorldPop catalog curates (flattened)."""
    return sorted(
        {
            sid
            for record in catalog.datasets.values()
            for sub in (getattr(record, "subaliases", None) or [])
            if (sid := getattr(sub, "id", None))
        }
    )


def _usgs_parameter_codes() -> list[str]:
    """Return every USGS parameter code from the live reference table (SDK)."""
    from dataretrieval import waterdata

    result = waterdata.get_reference_table(collection="parameter-codes")
    frame = result[0] if isinstance(result, tuple) else result
    return [str(code) for code in frame["parameter_code"]]


def _usgs_water_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every USGS parameter code, live (public `dataretrieval` SDK).

    Args:
        catalog: The loaded USGS Water `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"usgs_water": [sorted parameter codes]}`.
    """
    return {"usgs_water": sorted(set(_usgs_parameter_codes()))}


def _usgs_parameter_rows() -> dict[str, dict[str, str]]:
    """Return the live USGS parameter table keyed by code (name/group/unit)."""
    from dataretrieval import waterdata

    result = waterdata.get_reference_table(collection="parameter-codes")
    frame = result[0] if isinstance(result, tuple) else result
    rows: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        code = str(
            row.get("parameter_code") or row.get("parameterCode") or row.get("id") or ""
        ).strip()
        if not code:
            continue
        rows[code] = {
            "name": str(row.get("parameter_name") or row.get("name") or ""),
            "group": str(row.get("parameter_group_code") or row.get("group") or ""),
            "unit": str(row.get("unit_of_measure") or row.get("unit") or ""),
        }
    return dict(sorted(rows.items()))


def _write_sibling_index(info: BackendInfo, filename: str, payload: Any) -> str:
    """Write an informational `available_*` index file next to the catalog.

    For the computed-index providers (openaq / worldpop / usgs_water) whose
    `available_*` attribute is derived from the curated rows at load time:
    `--write` persists the *full* live universe to a sibling YAML the
    runtime does not load (the maintainer / docs artefact the tools wrote).

    Args:
        info: The backend whose catalog directory receives the sibling.
        filename: The sibling file name (e.g. `available_parameters.yaml`).
        payload: The mapping to dump (already keyed by its block name).

    Returns:
        The path of the sibling index written.
    """
    path = _index_path(info).parent / filename
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def _write_usgs_water(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite USGS Water's sibling `available_parameters.yaml` (full table)."""
    return _write_sibling_index(
        info,
        "available_parameters.yaml",
        {"available_parameters": _usgs_parameter_rows()},
    )


def _write_worldpop(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite WorldPop's sibling `available_products.yaml` (alias -> sub-aliases)."""
    return _write_sibling_index(
        info, "available_products.yaml", {"available_products": grouped}
    )


def _write_openaq(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite OpenAQ's sibling `available_parameters.yaml` (full live list)."""
    return _write_sibling_index(
        info, "available_parameters.yaml", {"available_parameters": _flatten(grouped)}
    )


def _get_text(url: str) -> str:
    """GET `url` and return the response body as text (raising on HTTP error)."""
    response = requests.get(url, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.text


#: NOAA HOMR registry of every WSR-88D / NEXRAD site (public, fixed-width).
_RADAR_STATIONS_URL = "https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt"


def _radar_column_spans(separator: str) -> list[tuple[int, int]]:
    """Return one `(start, end)` slice per dash-run column in the rule line."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, char in enumerate(separator):
        if char == "-" and start is None:
            start = index
        elif char != "-" and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(separator)))
    return spans


def _radar_station_rows(text: str) -> dict[str, dict[str, Any]]:
    """Parse the HOMR `nexrad-stations.txt` body into full station rows.

    The file is a fixed-width table: a header row, a row of dash runs
    marking each column's span, then one row per site. Keeps the four-letter
    alphabetic ICAO sites with in-range coordinates — the shape of the
    catalog's `stations:` block.

    Args:
        text: The full `nexrad-stations.txt` body.

    Returns:
        Mapping of ICAO id to `{name, latitude, longitude, state}`, sorted.
    """
    lines = text.splitlines()
    if len(lines) < 3:
        return {}
    spans = _radar_column_spans(lines[1])
    columns = {lines[0][s:e].strip(): (s, e) for s, e in spans}
    if "ICAO" not in columns:
        return {}

    def cell(row: str, name: str) -> str:
        start, end = columns[name]
        return row[start:end].strip()

    rows: dict[str, dict[str, Any]] = {}
    for row in lines[2:]:
        icao = cell(row, "ICAO")
        if len(icao) != 4 or not icao.isalpha():
            continue
        try:
            lat = round(float(cell(row, "LAT")), 4)
            lon = round(float(cell(row, "LON")), 4)
        except (ValueError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        rows[icao] = {
            "name": cell(row, "NAME").title(),
            "latitude": lat,
            "longitude": lon,
            "state": cell(row, "ST") if "ST" in columns else "",
        }
    return dict(sorted(rows.items()))


def _radar_station_ids(text: str) -> list[str]:
    """Return the sorted ICAO ids from the HOMR table (id column only)."""
    return sorted(_radar_station_rows(text))


def _radar_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every live NEXRAD ICAO id from the public NOAA HOMR registry.

    Args:
        catalog: The loaded radar `Catalog` (unused; the registry is fixed).

    Returns:
        A single-group mapping `{"radar": [sorted ICAO ids]}`.
    """
    return {"radar": _radar_station_ids(_get_text(_RADAR_STATIONS_URL))}


def _write_radar(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Regenerate radar's curated `stations:` block from NOAA HOMR.

    Unlike the `available_*` writers, this rewrites the *curated* station
    registry itself — re-parsing the HOMR table into full `{name, latitude,
    longitude, state}` rows (the radar catalog has no separate index; its
    `stations:` map is the catalog).

    Args:
        info: The radar backend.
        grouped: The live id fetch (unused; the full table is re-fetched).

    Returns:
        The path of the rewritten catalog file.
    """
    path = _index_path(info)
    _replace_index_block(
        path, "stations", _radar_station_rows(_get_text(_RADAR_STATIONS_URL))
    )
    return str(path)


#: FIRMS data-availability listing of every served sensor (needs a MAP_KEY).
_FIRMS_DATA_AVAIL_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{map_key}/all"
)

#: data_availability also lists burned-area products that are not area-CSV
#: active-fire sources; they belong to the GEE backend, not the catalog.
_FIRMS_EXCLUDED = frozenset({"BA_MODIS", "BA_VIIRS"})


def _firms_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every live FIRMS sensor id from the data_availability endpoint.

    Reads the `FIRMS_MAP_KEY` from the environment (without it the request
    fails and `refresh_one` reports an `"error"`), then parses the
    `data_id` column, dropping the burned-area products the catalog
    deliberately excludes.

    Args:
        catalog: The loaded FIRMS `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"firms": [sorted sensor ids]}`.
    """
    key = os.environ.get("FIRMS_MAP_KEY", "")
    text = _get_text(_FIRMS_DATA_AVAIL_URL.format(map_key=key))
    rows = text.splitlines()
    if not rows or not rows[0].lower().startswith("data_id"):
        raise RuntimeError(f"data_availability returned a non-CSV body: {text[:120]}")
    ids = {
        code
        for row in rows[1:]
        if (code := row.split(",", 1)[0].strip()) and code not in _FIRMS_EXCLUDED
    }
    return {"firms": sorted(ids)}


def _fdsn_provider_ids() -> list[str]:
    """Return every FDSN provider id obspy can reach (`URL_MAPPINGS` keys)."""
    from obspy.clients.fdsn.header import URL_MAPPINGS

    return [str(name) for name in URL_MAPPINGS]


def _fdsn_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every FDSN provider id obspy can reach (SDK enum, no network).

    The universe is obspy's `URL_MAPPINGS` registry — the same source the
    curated providers are drawn from — so a diff surfaces FDSN data centres
    obspy has gained or dropped since the catalog was curated.

    Args:
        catalog: The loaded FDSN `Catalog` (unused; obspy is the source).

    Returns:
        A single-group mapping `{"fdsn": [sorted provider ids]}`.
    """
    return {"fdsn": sorted(set(_fdsn_provider_ids()))}


def _overture_release_ids() -> list[str]:
    """Return every available Overture release id (`overturemaps` SDK).

    `get_available_releases()` returns a `(all_releases, latest)` tuple;
    only the release list is taken.
    """
    from overturemaps.core import get_available_releases

    result = get_available_releases()
    releases = result[0] if isinstance(result, tuple) else result
    return [str(release) for release in releases]


def _overture_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every available Overture release via the `overturemaps` SDK.

    Overture's refreshable axis is its *releases* (date-stamped data
    versions), not the fixed theme/type set — so this diffs against the
    catalog's `available_releases:` index, not `available_datasets:`.

    Args:
        catalog: The loaded Overture `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"overture": [sorted release ids]}`.
    """
    return {"overture": sorted(set(_overture_release_ids()))}


#: CHC anonymous-FTP host and the products root walked for coverage.
_CHC_FTP_HOST = "data.chc.ucsb.edu"
_CHC_ROOT = "pub/org/chc/products"
#: How far the BFS descends below the root before giving up on a branch.
_CHC_MAX_DEPTH = 6
#: Suffixes that mark a leaf "data file" (so its directory is a product dir).
_CHC_DATA_SUFFIXES = (
    ".tif",
    ".tif.gz",
    ".tiff",
    ".nc",
    ".nc4",
    ".bil",
    ".bil.gz",
    ".bin",
    ".cog",
    ".png",
    ".grb",
    ".grib",
)
_CHC_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _chc_is_product_listing(entries: list[str]) -> bool:
    """Return whether a directory listing marks a CHC product directory.

    A product directory is one whose children are data files (`.tif`,
    `.nc`, `.bil`, ...) or year-named subdirectories; anything else is an
    intermediate directory to descend into.

    Args:
        entries: The directory's child names.

    Returns:
        `True` if the listing looks like a product directory.
    """
    has_data = any(name.lower().endswith(_CHC_DATA_SUFFIXES) for name in entries)
    has_years = any(_CHC_YEAR_RE.fullmatch(name) for name in entries)
    return has_data or has_years


def _chc_walk(ftp: FTP, root: str, max_depth: int) -> list[str]:
    """BFS-walk `root` and return every discovered CHC product directory.

    Mirrors `tools/chc/refresh_chc_catalog.py`: descends intermediate
    directories until a product directory is reached or `max_depth` levels
    below `root`. Unreachable / permission-denied directories are skipped.

    Args:
        ftp: A logged-in FTP connection.
        root: The products root to walk from (no trailing slash).
        max_depth: Maximum levels to descend below `root`.

    Returns:
        The sorted product-directory paths (each `.../`-terminated).
    """
    discovered: list[str] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    while queue:
        path, depth = queue.pop(0)
        try:
            ftp.cwd("/")
            ftp.cwd(path)
            entries = sorted(ftp.nlst())
        except (error_perm, OSError):
            continue
        if _chc_is_product_listing(entries):
            discovered.append(path.rstrip("/") + "/")
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if "." in entry:  # an unrecognised file (e.g. README.txt)
                continue
            queue.append((f"{path.rstrip('/')}/{entry}/", depth + 1))
    return sorted(discovered)


def _chc_discovered_paths() -> list[str]:
    """Return every CHC product directory from a live anonymous-FTP walk."""
    with FTP(_CHC_FTP_HOST, timeout=_TIMEOUT) as ftp:  # nosec B321
        ftp.login()
        return _chc_walk(ftp, _CHC_ROOT, _CHC_MAX_DEPTH)


def _chc_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every CHC product directory from the live FTP tree (anonymous).

    CHC's refreshable axis is the set of FTP product directories, diffed
    against the distinct `ftp_bases` the catalog references (see
    :func:`_chc_ftp_bases`) — not the hand-curated `available_datasets:`
    slugs, which are a human-curation artefact the diff cannot derive.

    Args:
        catalog: The loaded CHC `Catalog` (unused; the FTP tree is the source).

    Returns:
        A single-group mapping `{"chc": [sorted product directories]}`.
    """
    return {"chc": sorted({p.rstrip("/") + "/" for p in _chc_discovered_paths()})}


def _chc_ftp_bases(catalog: Any) -> list[str]:
    """Return the distinct `ftp_bases` paths the CHC catalog references."""
    return sorted(
        {
            base.rstrip("/") + "/"
            for dataset in catalog.datasets.values()
            for base in dataset.ftp_bases.values()
        }
    )


#: JRC 54009 land tile-schema shapefile (the GHSL Mollweide tile grid source).
_GHSL_TILE_SCHEMA_ZIP = (
    "https://ghsl.jrc.ec.europa.eu/download/GHSL_data_54009_shapefile.zip"
)


def _ghsl_tile_frame() -> Any:
    """Download the JRC tile shapefile and return its `(tile_id, bounds)` frame.

    Fetches the JRC 54009 land tile-schema zip, extracts it to a temp dir,
    reads the `*tile_schema_land*.shp` with geopandas, and keeps the tile id,
    integer bounds, and geometry. (GIS read kept local to this maintainer op.)

    Returns:
        A `geopandas.GeoDataFrame` of the tile grid.
    """
    import io
    import tempfile
    import zipfile

    import geopandas as gpd

    response = requests.get(_GHSL_TILE_SCHEMA_ZIP, timeout=120)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as workdir:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            archive.extractall(workdir)  # nosec B202 — trusted JRC zip
        shapefile = next(Path(workdir).glob("*tile_schema_land*.shp"))
        frame = gpd.read_file(shapefile)[
            ["tile_id", "left", "top", "right", "bottom", "geometry"]
        ]
    for column in ("left", "top", "right", "bottom"):
        frame[column] = frame[column].astype(int)
    return frame


def refresh_ghsl_tiles() -> tuple[str, int]:
    """Regenerate GHSL's bundled `tile_schema.geojson` from the JRC shapefile.

    The GIS analogue of an `available_*` refresh: rewrites the 18x36 Mollweide
    tile index the GHSL backend reads to map a bbox to its covering tiles.

    Returns:
        `(written_path, tile_count)`.
    """
    from earthlens.ghsl._helpers import TILE_SCHEMA_PATH

    frame = _ghsl_tile_frame()
    frame.to_file(TILE_SCHEMA_PATH, driver="GeoJSON")
    return str(TILE_SCHEMA_PATH), len(frame)


#: Provider id -> a callable regenerating a bundled GIS artefact (not an
#: `available_*` index). Surfaced by `refresh <provider> --tiles`.
_TILE_REGENS: dict[str, Callable[[], tuple[str, int]]] = {"ghsl": refresh_ghsl_tiles}


#: Provider id -> a callable taking the loaded catalog and returning its
#: live ids grouped (e.g. per STAC endpoint). Public providers need no
#: credentials; credentialed ones (openaq, firms) read their key from the env.
_REFRESHERS: dict[str, Callable[[Any], dict[str, list[str]]]] = {
    "stac": _stac_grouped,
    "ecmwf": _ecmwf_grouped,
    "openeo": _openeo_grouped,
    "hdx": _hdx_grouped,
    "earthdata": _earthdata_grouped,
    "openaq": _openaq_grouped,
    "cmems": _cmems_grouped,
    "eumetsat": _eumetsat_grouped,
    "sentinel_hub": _sentinel_hub_grouped,
    "gee": _gee_grouped,
    "worldpop": _worldpop_grouped,
    "usgs_water": _usgs_water_grouped,
    "radar": _radar_grouped,
    "firms": _firms_grouped,
    "fdsn": _fdsn_grouped,
    "overture": _overture_grouped,
    "chc": _chc_grouped,
}

#: Provider id -> a callable that persists a grouped live fetch back into
#: the bundled catalog (the `--write` half). A subset of `_REFRESHERS`:
#: providers whose informational index is computed from the curated rows at
#: load time (openaq, worldpop, usgs_water) have no on-disk block to rewrite
#: and intentionally report "live read only" instead.
_WRITERS: dict[str, Callable[[BackendInfo, dict[str, list[str]]], str]] = {
    "stac": _write_stac,
    "ecmwf": _index_writer("available_datasets"),
    "openeo": _write_openeo,
    "cmems": _index_writer("available_datasets"),
    "eumetsat": _index_writer("available_datasets"),
    "sentinel_hub": _index_writer("available_collections"),
    "gee": _index_writer("available_datasets"),
    "earthdata": _index_writer("available_datasets"),
    "hdx": _write_hdx,
    "overture": _index_writer("available_releases"),
    "radar": _write_radar,
    "usgs_water": _write_usgs_water,
    "worldpop": _write_worldpop,
    "openaq": _write_openaq,
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


def _curated_releases(catalog: Any) -> list[str]:
    """Return the Overture releases the catalog tracks (its refresh axis)."""
    return sorted(
        str(release) for release in getattr(catalog, "available_releases", [])
    )


#: Provider id -> a callable returning the upstream ids the catalog curates
#: (for the `audit` drift check). Falls back to the dataset keys otherwise.
_CURATED_IDS: dict[str, Callable[[Any], list[str]]] = {
    "stac": _curated_collection_ids,
    "openeo": _curated_collection_ids,
    "hdx": _curated_attr_ids("hdx_id"),
    "earthdata": _curated_attr_ids("short_name"),
    "eumetsat": _curated_collection_ids,
    "sentinel_hub": _curated_attr_ids("sh_collection"),
    "worldpop": _worldpop_curated_ids,
    "usgs_water": _curated_attr_ids("code"),
    "fdsn": _curated_attr_ids("fdsn_id"),
    "overture": _curated_releases,
    "chc": _chc_ftp_bases,
}

#: Provider id -> the catalog attribute holding its persisted informational
#: index. Defaults to `available_datasets`; Overture's refreshable axis is
#: its date-stamped `available_releases` instead.
_INDEX_ATTR: dict[str, str] = {"overture": "available_releases"}

#: Provider id -> a callable computing the bundled ids to diff against, for
#: backends whose refresh axis is neither `available_datasets` nor a simple
#: attribute. CHC diffs the live FTP tree against its `ftp_bases` paths (not
#: the hand-curated `available_datasets:` slugs the diff cannot derive).
_BUNDLED_IDS: dict[str, Callable[[Any], list[str]]] = {"chc": _chc_ftp_bases}


def _bundled_ids(catalog: Any, provider: str) -> list[str]:
    """Return the bundled ids a provider's live fetch is diffed against.

    Resolution order: an explicit `_BUNDLED_IDS` resolver (a computed axis
    such as CHC's `ftp_bases`); else the persisted informational index
    (`available_datasets`, or Overture's `available_releases`); else, for a
    backend whose `datasets` map *is* the index (radar / firms / fdsn), the
    curated ids (`_CURATED_IDS`) or, failing that, the dataset keys.

    Args:
        catalog: The loaded provider catalog.
        provider: The canonical provider id.

    Returns:
        The id list to diff the live fetch against.
    """
    custom = _BUNDLED_IDS.get(provider)
    if custom:
        return custom(catalog)
    persisted = [
        str(value)
        for value in getattr(
            catalog, _INDEX_ATTR.get(provider, "available_datasets"), []
        )
        or []
    ]
    if persisted:
        return persisted
    resolver = _CURATED_IDS.get(provider)
    return resolver(catalog) if resolver else [str(key) for key in catalog.datasets]


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
    index_attr = _INDEX_ATTR.get(info.provider, "available_datasets")
    available = {str(ident) for ident in getattr(catalog, index_attr, [])}
    return AuditOutcome(
        provider=info.provider,
        status="ok",
        live_count=len(live),
        curated_count=len(curated),
        broken=sorted(curated - live),
        # Untracked = live ids earthlens tracks nowhere — neither curated nor
        # in the available index (so a provider whose index lives elsewhere,
        # like openaq's `parameters`, doesn't report its curated rows as drift).
        untracked=sorted(live - available - curated),
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
    bundled = _bundled_ids(catalog, info.provider)
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
