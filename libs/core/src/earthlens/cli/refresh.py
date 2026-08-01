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
from typing import Any, cast

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
    return cast("dict[str, Any]", response.json())


def _redact(text: str, secret: str) -> str:
    """Mask `secret` (e.g. an API key) wherever it appears in `text`.

    Used to scrub a credential out of an error message before it is surfaced
    in an outcome `detail` — some providers (FIRMS) carry the key in the
    request URL, which `requests` echoes verbatim in `HTTPError`.

    Args:
        text: The message that may contain the secret.
        secret: The secret to mask (a no-op when empty).

    Returns:
        `text` with every occurrence of `secret` replaced by `***`.

    Examples:
        - A key embedded in a URL is masked:

            ```python
            >>> from earthlens.cli.refresh import _redact
            >>> _redact("for url: https://x/csv/SEKRET/all", "SEKRET")
            'for url: https://x/csv/***/all'

            ```
        - An empty secret leaves the text untouched:

            ```python
            >>> from earthlens.cli.refresh import _redact
            >>> _redact("nothing to hide", "")
            'nothing to hide'

            ```
    """
    return text.replace(secret, "***") if secret else text


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
    return cast("Path", base / "_index.yaml" if base.is_dir() else base)


def _replace_index_block(path: Path, block_key: str, payload: Any) -> None:
    """Replace exactly one top-level block of a YAML index in place.

    Rewrites the `{block_key}:` block (from its key line up to the next
    column-zero key, or end of file) with `payload`, leaving every other
    block byte-for-byte intact — including the header comments above the
    block and any comment / blank lines that sit immediately above the next
    block (those belong to *it* and are preserved, not swallowed). This is
    what lets a provider whose `_index.yaml` holds more than one block
    (e.g. openEO's `available_collections:` *and* `available_processes:`)
    be rewritten without disturbing the sibling block or its comments.

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
    # Comment / blank lines immediately above the next block belong to *it*,
    # not to the block being replaced — back the cut up over them so they are
    # preserved rather than swallowed into the rewritten span.
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
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
        """Rewrite `info`'s index block from the live ids; return the file path."""
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
    params: dict[str, str | int] = {"provider": provider, "page_size": 2000}
    response = requests.get(
        _CMR_COLLECTIONS_URL,
        params=params,
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
        except Exception:  # noqa: BLE001 — skip an unreachable sub-catalog  # nosec B112
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


#: Public per-asset STAC-doc URL base (the id -> doc filename convention).
_GEE_STAC_DOC_BASE = "https://storage.googleapis.com/earthengine-stac/catalog"


def _gee_stac_or_none(asset_id: str) -> dict[str, Any] | None:
    """Fetch one Earth Engine asset's public STAC document, or None on error.

    Args:
        asset_id: The Earth Engine asset id (e.g. `LANDSAT/LC09/C02/T1_L2`).

    Returns:
        The parsed STAC document, or None when it 404s / is unreadable.
    """
    provider = asset_id.split("/", 1)[0]
    url = f"{_GEE_STAC_DOC_BASE}/{provider}/{asset_id.replace('/', '_')}.json"
    try:
        return _get_json(url)
    except Exception:  # noqa: BLE001 — a missing/unreadable doc -> "missing"
        return None


def _gee_classify(asset_id: str, curated: set[str]) -> str:
    """Bucket one asset id for the curation-coverage report.

    Args:
        asset_id: The Earth Engine asset id to classify.
        curated: The set of asset ids already in the curated `datasets:` map.

    Returns:
        One of `"DONE"` (already curated), `"table"` (a FeatureCollection,
        out of raster scope), `"addressable"` (has bands carrying usable
        metadata — a `gee:units` / `gee:scale`), `"thin"` (no usable band
        metadata, needs hand-modelling), or `"missing"` (no STAC doc).
    """
    if asset_id in curated:
        return "DONE"
    doc = _gee_stac_or_none(asset_id)
    if doc is None:
        return "missing"
    if doc.get("gee:type") == "table":
        return "table"
    bands = (doc.get("summaries", {}) or {}).get("eo:bands") or []
    has_meta = any(b.get("gee:units") or b.get("gee:scale") is not None for b in bands)
    return "addressable" if (bands and has_meta) else "thin"


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
    catalog's `stations:` block. Returns an empty mapping (rather than
    raising) when the table is too short or its header lacks any of the
    required `ICAO` / `NAME` / `LAT` / `LON` columns.

    Args:
        text: The full `nexrad-stations.txt` body.

    Returns:
        Mapping of ICAO id to `{name, latitude, longitude, state}`, sorted
        (`state` is `""` when the table carries no `ST` column).
    """
    lines = text.splitlines()
    if len(lines) < 3:
        return {}
    spans = _radar_column_spans(lines[1])
    columns = {lines[0][s:e].strip(): (s, e) for s, e in spans}
    # ICAO / NAME / LAT / LON are read unconditionally below; bail cleanly if
    # the upstream table ever drops one rather than raising a KeyError.
    if not {"ICAO", "NAME", "LAT", "LON"} <= set(columns):
        return {}

    def cell(row: str, name: str) -> str:
        """Return the stripped value of the fixed-width `name` column in `row`."""
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
    deliberately excludes. The key is carried in the request URL path, so a
    failed fetch is re-raised with the key masked (via :func:`_redact`) — it
    must never reach the surfaced `detail`.

    Args:
        catalog: The loaded FIRMS `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"firms": [sorted sensor ids]}`.

    Raises:
        RuntimeError: If the data_availability fetch fails; the message has
            the `FIRMS_MAP_KEY` redacted.
    """
    key = os.environ.get("FIRMS_MAP_KEY", "")
    try:
        text = _get_text(_FIRMS_DATA_AVAIL_URL.format(map_key=key))
    except Exception as exc:  # noqa: BLE001 — scrub the key from the URL in the error
        raise RuntimeError(_redact(str(exc), key)) from None
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


def _jaxa_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every live JAXA dataset id from all three protocols.

    Walks the two SDKs' authoritative listings: `jaxa.earth`'s STAC
    catalog (118 COG collections at A1 capture time) for the
    `jaxa-earth` group, and `gportal.datasets()` (799 numeric dataset
    ids at A1 capture time) for the `gportal` group. The `ptree` group
    is derived **live** from the bundled JAXA catalog's `ptree` rows
    (their `short_name` field) — P-Tree has no discoverable listing
    endpoint, so the local catalog is the authoritative source; a
    curator adding a new `protocol: ptree` row therefore reaches
    `_index.yaml` automatically on the next `refresh --write`.

    Args:
        catalog: The loaded JAXA `Catalog`. Used to derive the `ptree`
            group; the `jaxa-earth` and `gportal` groups still come
            from the SDKs.

    Returns:
        Three-group mapping:
        `{"jaxa-earth": [STAC collection ids], "gportal": [numeric
        ids], "ptree": [product tokens]}`.
    """
    import gportal as _gportal  # type: ignore[import-not-found]
    from jaxa.earth import je as _je  # type: ignore[import-not-found]

    je_ids, _ = _je.ImageCollectionList().filter_name()
    gp_tree = _gportal.datasets()

    gp_ids: list[str] = []
    stack: list[Any] = [gp_tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            gp_ids.extend(str(x) for x in node)
    ptree_ids = sorted(
        {
            row.short_name
            for row in catalog.datasets.values()
            if row.protocol == "ptree" and row.short_name
        }
    )
    return {
        "jaxa-earth": sorted(set(str(c) for c in je_ids)),
        "gportal": sorted(set(gp_ids)),
        "ptree": ptree_ids,
    }


#: Region the unsigned NWM operational bucket is listed from.
_NWM_REGION = "us-east-1"

#: A trailing `_mem<N>` ensemble-member directory suffix (e.g.
#: `medium_range_mem3`) — collapsed back to its base configuration key.
_NWM_MEMBER_RE = re.compile(r"_mem\d+$")


def _nwm_collapse_member(directory: str) -> str:
    """Collapse an NWM ensemble-member directory to its configuration key.

    The operational bucket publishes each ensemble member under its own
    `{config}_mem<N>` directory; the curated catalog keys an ensemble by its
    bare `{config}` name. Stripping the `_mem<N>` suffix maps a live
    directory back into the curated namespace so the diff lines up.

    Args:
        directory: A live configuration directory name.

    Returns:
        The base configuration key (unchanged when not a member directory).

    Examples:
        - An ensemble member collapses to its base configuration:

            ```python
            >>> from earthlens.cli.refresh import _nwm_collapse_member
            >>> _nwm_collapse_member("medium_range_mem3")
            'medium_range'

            ```
        - A non-ensemble directory is returned unchanged:

            ```python
            >>> from earthlens.cli.refresh import _nwm_collapse_member
            >>> _nwm_collapse_member("short_range")
            'short_range'

            ```
    """
    return _NWM_MEMBER_RE.sub("", directory)


def _nwm_unsigned_client() -> Any:
    """Return an unsigned `boto3` S3 client for the public `noaa-nwm-pds` bucket.

    The single place the NWM CLI tooling (`refresh` here, `validate --live` in
    :mod:`earthlens.cli.validate`) builds its anonymous client, so the region
    and signature config live in one home.

    Returns:
        An unsigned `boto3` S3 client.
    """
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    return boto3.client(
        "s3", region_name=_NWM_REGION, config=Config(signature_version=UNSIGNED)
    )


def _nwm_latest_complete_day(client: Any) -> str:
    """Return the most recent *complete* `nwm.YYYYMMDD/` day prefix.

    Lists the `nwm.YYYYMMDD/` date prefixes and returns the day before the
    latest (the newest prefix may be mid-publication), falling back to the
    only day when just one is present. Shared by the NWM refresh and
    `validate --live` walks so the "complete day" heuristic lives in one home.

    Args:
        client: An unsigned S3 client (see :func:`_nwm_unsigned_client`).

    Returns:
        The selected `nwm.YYYYMMDD` prefix (no trailing slash).

    Raises:
        RuntimeError: If the bucket exposes no `nwm.YYYYMMDD/` date prefix.
    """
    from earthlens.nwm import BUCKET

    paginator = client.get_paginator("list_objects_v2")
    days = sorted(
        prefix.rstrip("/")
        for page in paginator.paginate(Bucket=BUCKET, Delimiter="/")
        for entry in page.get("CommonPrefixes", [])
        if (prefix := entry["Prefix"]).startswith("nwm.")
    )
    if not days:
        raise RuntimeError(f"no nwm.YYYYMMDD/ prefixes found on {BUCKET}")
    return cast("str", days[-2] if len(days) > 1 else days[-1])


def _nwm_config_dirs(client: Any, day: str) -> list[str]:
    """Return the configuration directory names published under one NWM day.

    Args:
        client: An unsigned S3 client (see :func:`_nwm_unsigned_client`).
        day: An `nwm.YYYYMMDD` prefix (see :func:`_nwm_latest_complete_day`).

    Returns:
        The raw configuration directory names (ensemble members still carry
        their `_mem<N>` suffix), sorted.
    """
    from earthlens.nwm import BUCKET

    result = client.list_objects_v2(Bucket=BUCKET, Prefix=f"{day}/", Delimiter="/")
    return sorted(
        entry["Prefix"].split("/")[1] for entry in result.get("CommonPrefixes", [])
    )


def _nwm_live_config_dirs() -> list[str]:
    """Return the configuration directories under the latest complete NWM day.

    Composes the shared NWM bucket primitives: an unsigned client
    (:func:`_nwm_unsigned_client`), the most recent complete day
    (:func:`_nwm_latest_complete_day`), and that day's configuration
    directories (:func:`_nwm_config_dirs`).

    Returns:
        The raw configuration directory names (ensemble members still carry
        their `_mem<N>` suffix).

    Raises:
        RuntimeError: If the bucket exposes no `nwm.YYYYMMDD/` date prefix.
    """
    client = _nwm_unsigned_client()
    return _nwm_config_dirs(client, _nwm_latest_complete_day(client))


def _nwm_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the live NWM configurations from the unsigned operational bucket.

    Walks the most recent complete `nwm.YYYYMMDD/` day on `noaa-nwm-pds`
    and collapses each ensemble-member directory to its base configuration
    key (see :func:`_nwm_collapse_member`), so the live set is diffed
    against the catalog's `available_configurations:` index in the same
    namespace. The assimilation-input `usgs_timeslices` directory is a live
    configuration the catalog deliberately does not curate, so it surfaces
    as an untracked id (informational), not drift.

    Args:
        catalog: The loaded NWM `Catalog` (unused; the bucket is the source).

    Returns:
        A single-group mapping `{"nwm": [sorted configuration keys]}`.
    """
    dirs = {_nwm_collapse_member(name) for name in _nwm_live_config_dirs()}
    return {"nwm": sorted(dirs)}


def _nwm_curated_configs(catalog: Any) -> list[str]:
    """Return the configuration keys the NWM catalog curates (its refresh axis)."""
    return sorted(catalog.configurations)


def _s3_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the S3 registry's dataset names (its `available_datasets` universe).

    The AWS Open-Data S3 backend has no single live "list all" endpoint — its
    universe *is* the curated registry — so the refresher returns the curated
    dataset names. `--write` then regenerates the in-file `available_datasets:`
    block from them (the `tools/s3/refresh_s3_catalog.py:refresh` step).

    Args:
        catalog: The loaded S3 `Catalog`.

    Returns:
        A single-group mapping `{"s3": [sorted registered dataset names]}`.
    """
    return {"s3": sorted(str(key) for key in catalog.datasets)}


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


def _erddap_dataset_ids(server_url: str) -> list[str]:
    """List every dataset id one ERDDAP server publishes.

    Reads the server's synthetic `allDatasets` table (every ERDDAP exposes
    it) via tabledap JSON, asking only for the `datasetID` column. The
    `allDatasets` meta-row that the table lists for itself is dropped.

    Args:
        server_url: An ERDDAP base URL (a trailing slash is tolerated).

    Returns:
        The server's dataset ids, sorted and de-duplicated.
    """
    base = server_url.rstrip("/")
    body = _get_json(f"{base}/tabledap/allDatasets.json?datasetID")
    rows = body["table"]["rows"]
    return sorted({row[0] for row in rows if row[0] != "allDatasets"})


def _erddap_grouped(catalog: Any) -> dict[str, list[str]]:
    """List every dataset on each ERDDAP server the catalog curates from.

    ERDDAP has no single global dataset universe — it is a protocol spoken
    by many independent servers — so the live "available" set is the union
    of the `allDatasets` table of each distinct `server_url` the curated
    rows reference. Grouped per server so the diff shows which server a new
    id came from.

    Fails fast if any curated server is unreachable (the `_get_json` error
    propagates and `refresh_one` / `audit_one` report `"error"`). This is
    deliberate: under `--write`, a partial crawl would rewrite `_index.yaml`
    without a down server's ids, and the loader's `curated ⊆ available`
    invariant would then reject that server's curated rows. The empty-fetch
    seatbelt only guards the all-servers-down case, so a partial result must
    abort rather than persist.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `{server_url: [dataset_id, …]}` for every distinct curated server.
    """
    servers = sorted({row.server_url for row in catalog.datasets.values()})
    return {server: _erddap_dataset_ids(server) for server in servers}


#: Provider id -> a callable regenerating a bundled GIS artefact (not an
#: `available_*` index). Surfaced by `refresh <provider> --tiles`.
_TILE_REGENS: dict[str, Callable[[], tuple[str, int]]] = {"ghsl": refresh_ghsl_tiles}


def _gbif_grouped(catalog: Any) -> dict[str, list[str]]:
    """List GBIF's curated biodiversity index — the universe IS the catalog.

    GBIF's backbone has 3+ billion occurrences across an unbounded taxonomic
    tree; no anonymous endpoint enumerates "every taxon", so the universe
    earthlens tracks is the curated `available_datasets:` index of common
    higher-rank taxa (kingdoms, classes, orders) plus the friendly aliases
    in `taxa:`. The refresher returns that combined sorted set so the audit
    can confirm nothing has been removed by hand.

    Args:
        catalog: The loaded GBIF `Catalog`.

    Returns:
        A single-group mapping `{"gbif": [sorted available + friendly ids]}`.
    """
    ids = set(catalog.available_datasets) | set(catalog.datasets)
    return {"gbif": sorted(ids)}


def _obis_grouped(catalog: Any) -> dict[str, list[str]]:
    """List OBIS's curated marine-taxa index — the universe IS the catalog.

    Like `_gbif_grouped`, OBIS has no anonymous "list every taxon" endpoint;
    the universe earthlens tracks is the curated `available_datasets:` index
    of common marine higher-rank taxa plus the friendly species aliases. The
    refresher returns that combined sorted set.

    Args:
        catalog: The loaded OBIS `Catalog`.

    Returns:
        A single-group mapping `{"obis": [sorted available + friendly ids]}`.
    """
    ids = set(catalog.available_datasets) | set(catalog.datasets)
    return {"obis": sorted(ids)}


def _wdpa_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the WDPA country axis — the universe IS the curated ISO3 set.

    Protected Planet v4 needs a `WDPA_TOKEN` (UNEP-WCMC manual approval) for
    every request, so refreshing against the live country dictionary is not
    a no-credential operation. The catalog mirrors the curated codes as the
    informational index and the refresher returns them — the same pattern as
    s3 / fdsn.

    Args:
        catalog: The loaded WDPA `Catalog`.

    Returns:
        A single-group mapping `{"wdpa": [sorted ISO3 codes]}`.
    """
    return {"wdpa": sorted(set(catalog.available_datasets) | set(catalog.datasets))}


def _iucn_grouped(catalog: Any) -> dict[str, list[str]]:
    """List the IUCN country axis — the universe IS the curated ISO2 set.

    Red List v4 needs an `IUCN_TOKEN` for every request, so the live country
    dictionary is not anonymous. The catalog mirrors the curated codes as
    the informational index and the refresher returns them — same pattern as
    s3 / wdpa.

    Args:
        catalog: The loaded IUCN `Catalog`.

    Returns:
        A single-group mapping `{"iucn": [sorted ISO2 codes]}`.
    """
    return {"iucn": sorted(set(catalog.available_datasets) | set(catalog.datasets))}


#: Zenodo's version-chain endpoint for one record.
_ZENODO_VERSIONS = "https://zenodo.org/api/records/{record}/versions"

#: Zenodo's search endpoint, used to discover Caravan extensions the catalog
#: does not yet know about. The community publishes new extensions as brand-new
#: records, so watching the pinned version chains alone would never find them.
_ZENODO_SEARCH = "https://zenodo.org/api/records"


def _caravan_grouped(catalog: Any) -> dict[str, list[str]]:
    """Report Caravan release drift: newer versions, and undiscovered extensions.

    Two kinds of drift matter for this backend and neither is visible from the
    curated rows alone:

    * A pinned extension gains a newer version. The catalog deliberately pins a
      **specific** version record rather than the moving concept DOI, so a new
      release is invisible until something looks. Worse, Caravan has changed
      *packaging* mid-life — GRDC went `.tar.gz` to `.zip` at v0.4 — which
      silently changes whether the archive can be range-read at all.
    * A brand-new community extension is published as its own record, so it
      never appears in any pinned version chain. The title search finds those.

    This reports; it never rewrites the catalog. Bumping a pin means re-checking
    the archive layout, gauge-id convention and column set, which is a human
    decision — hence no `_WRITERS` entry, so `--write` says "live read only".

    Args:
        catalog: The loaded Caravan `Catalog`.

    Returns:
        One group per extension holding only the releases published *after* its
        pin (empty when current), plus a `discovered` group naming Caravan
        records whose concept the catalog does not track at all.
    """
    grouped: dict[str, list[str]] = {}
    pinned: set[str] = set()
    known_concepts = {
        str(extension.concept_doi).rsplit(".", 1)[-1]
        for extension in catalog.datasets.values()
        if extension.concept_doi
    }
    for key, extension in sorted(catalog.datasets.items()):
        newer: set[str] = set()
        # Compare against the NEWEST pin, not each one. `base` pins both 1.6 and
        # the older range-readable 1.2, and every release between them would
        # otherwise be reported as drift when the row is in fact current.
        latest_pin = max(
            (version.release_date for version in extension.versions.values()),
            default="",
        )
        records = {
            archive.record
            for version in extension.versions.values()
            for archive in version.files.values()
        }
        pinned.update(str(record) for record in records)
        for record in sorted(records):
            payload = _get_json(_ZENODO_VERSIONS.format(record=record))
            for hit in (payload.get("hits") or {}).get("hits") or []:
                published = str((hit.get("metadata") or {}).get("publication_date", ""))
                if published > latest_pin and str(hit.get("id")) not in pinned:
                    newer.add(f"{hit.get('id')} ({published})")
        grouped[key] = sorted(newer)

    # Records the catalog deliberately does not wrap. Without this they surface
    # as "discovered" on every run, which trains the reader to ignore the signal.
    known_unsupported = {
        str(entry.get("records") or []) and str(record)
        for entry in (catalog.available_datasets or [])
        if isinstance(entry, dict)
        for record in (entry.get("records") or [])
    }
    discovered: set[str] = set()
    for query in ('title:"Caravan extension"', "title:Caravan AND keywords:Hydrology"):
        payload = _get_json(
            _ZENODO_SEARCH, params={"q": query, "size": 25, "sort": "newest"}
        )
        for hit in (payload.get("hits") or {}).get("hits") or []:
            record = str(hit.get("id"))
            concept = str(hit.get("conceptrecid"))
            if (
                record in pinned
                or concept in known_concepts
                or record in known_unsupported
            ):
                continue
            discovered.add(
                f"{record} ({(hit.get('metadata') or {}).get('title', '')[:60]})"
            )
    grouped["discovered"] = sorted(discovered)
    return grouped


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
    "s3": _s3_grouped,
    "nwm": _nwm_grouped,
    "jaxa": _jaxa_grouped,
    "erddap": _erddap_grouped,
    "gbif": _gbif_grouped,
    "obis": _obis_grouped,
    "wdpa": _wdpa_grouped,
    "iucn": _iucn_grouped,
    "caravan": _caravan_grouped,
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
    "s3": _index_writer("available_datasets"),
    "usgs_water": _write_usgs_water,
    "worldpop": _write_worldpop,
    "openaq": _write_openaq,
    # JAXA's catalog YAML carries an `available_datasets:` block that lists
    # every live id across all three protocols (jaxa-earth + gportal +
    # ptree). The flatten=True path unions the three protocol groups
    # into a single sorted list; the curated `datasets:` block stays
    # hand-authored. The `ptree` group is derived from the catalog's
    # ptree rows (see `_jaxa_grouped`) since P-Tree has no discoverable
    # listing endpoint of its own.
    "jaxa": _index_writer("available_datasets"),
    # ERDDAP's `_index.yaml` carries an `available_datasets:` block listing
    # every dataset id across the curated servers; the flatten path unions
    # the per-server groups. The curated `datasets:` rows stay hand-authored
    # in the per-slice YAML files.
    "erddap": _index_writer("available_datasets"),
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
        """Return the catalog's sorted, de-duplicated `attr` upstream ids."""
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
def _biodiversity_curated_ids(catalog: Any) -> list[str]:
    """Return the curated `available_datasets` index a cluster catalog tracks.

    Used by gbif / obis / wdpa / iucn — their refresh axis is the curated
    index plus the friendly aliases combined, which is what `_*_grouped`
    above also returns, so audit reports zero drift on a clean catalog.

    Args:
        catalog: The loaded cluster `Catalog`.

    Returns:
        The combined sorted set of `available_datasets` + `datasets` keys.
    """
    return sorted(set(catalog.available_datasets) | set(catalog.datasets))


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
    "nwm": _nwm_curated_configs,
    "gbif": _biodiversity_curated_ids,
    "obis": _biodiversity_curated_ids,
    "wdpa": _biodiversity_curated_ids,
    "iucn": _biodiversity_curated_ids,
}

#: Provider id -> the catalog attribute holding its persisted informational
#: index. Defaults to `available_datasets`; Overture's refreshable axis is
#: its date-stamped `available_releases`, NWM's is its `available_configurations`.
_INDEX_ATTR: dict[str, str] = {
    "overture": "available_releases",
    "nwm": "available_configurations",
}

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


#: The fixed coverage buckets a curation-coverage classifier reports, in
#: display order.
_COVERAGE_BUCKETS = ("DONE", "addressable", "thin", "table", "missing")


@dataclass
class CoverageOutcome:
    """The result of classifying a provider's available universe for curation.

    Distinct from :class:`AuditOutcome` (drift of curated-vs-live): coverage
    answers "of everything the provider exposes, how much is curated, and
    what is worth curating next".

    Attributes:
        provider: Canonical provider id.
        status: `"ok"`, `"unsupported"` (no classifier), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        counts: Per-bucket counts (see :data:`_COVERAGE_BUCKETS`).
        todo: The `addressable`-but-not-yet-curated ids worth curating next.
    """

    provider: str
    status: str
    detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    todo: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Project the coverage outcome to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The per-bucket counts ride along under `counts`:

                ```python
                >>> from earthlens.cli.refresh import CoverageOutcome
                >>> CoverageOutcome("gee", "ok", counts={"DONE": 3}).to_dict()["counts"]
                {'DONE': 3}

                ```
        """
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "counts": self.counts,
            "todo": self.todo,
        }


def _gee_coverage(catalog: Any) -> tuple[dict[str, int], list[str]]:
    """Classify every `available_datasets:` id of the GEE catalog.

    Args:
        catalog: The loaded GEE `Catalog`.

    Returns:
        `(counts, todo)` — per-bucket counts and the sorted `addressable`
        ids not yet curated.

    Raises:
        ValueError: If the `available_datasets:` index is empty.
    """
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    if not available:
        raise ValueError(
            "available_datasets: is empty — run `refresh gee --write` first"
        )
    curated = set(catalog.datasets)
    buckets: dict[str, list[str]] = {}
    for asset_id in available:
        buckets.setdefault(_gee_classify(asset_id, curated), []).append(asset_id)
    counts = {bucket: len(buckets.get(bucket, [])) for bucket in _COVERAGE_BUCKETS}
    return counts, sorted(buckets.get("addressable", []))


def _erddap_structures(catalog: Any) -> dict[str, str]:
    """Map every dataset id to its `dataStructure` across the curated servers.

    Reads each distinct curated `server_url`'s `allDatasets` table asking for
    `datasetID,dataStructure`, so the coverage classifier can tell a griddap
    (`"grid"`) dataset from a tabledap (`"table"`) one without a per-id query.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `{dataset_id: "grid" | "table"}` across every curated server.
    """
    servers = sorted({row.server_url for row in catalog.datasets.values()})
    structures: dict[str, str] = {}
    for server in servers:
        base = server.rstrip("/")
        body = _get_json(f"{base}/tabledap/allDatasets.json?datasetID,dataStructure")
        for dataset_id, structure in body["table"]["rows"]:
            if dataset_id != "allDatasets":
                structures[dataset_id] = structure
    return structures


def _erddap_classify(dataset_id: str, structure: str | None, curated: set[str]) -> str:
    """Bucket one ERDDAP dataset id by curation status and data structure.

    * `DONE` — already in the curated `datasets:` map.
    * `thin` — an ERDDAP test / demo dataset, detected by the heuristic
      `id.lower().startswith("test")` (ERDDAP's convention for its `testGrid…`
      / `testTable…` fixtures). A legitimately `test`-prefixed dataset would
      be misclassified here, but coverage is advisory only.
    * `addressable` — a `grid` (griddap) dataset, the raster universe worth
      curating next.
    * `table` — a `table` (tabledap) dataset, the separate tabular track.
    * `missing` — in the index but the server no longer lists a structure.

    Args:
        dataset_id: The ERDDAP dataset id.
        structure: Its `dataStructure` (`"grid"` / `"table"`), or `None`.
        curated: The set of already-curated dataset ids.

    Returns:
        The bucket name (one of :data:`_COVERAGE_BUCKETS`).
    """
    if dataset_id in curated:
        return "DONE"
    if dataset_id.lower().startswith("test"):
        return "thin"
    if structure == "grid":
        return "addressable"
    if structure == "table":
        return "table"
    return "missing"


def _erddap_coverage(catalog: Any) -> tuple[dict[str, int], list[str]]:
    """Classify every `available_datasets:` id of the ERDDAP catalog.

    griddap (`grid`) datasets are the addressable raster universe worth
    curating next; tabledap (`table`) datasets are the separate tabular
    track; ERDDAP test/demo ids are thin; an id the server no longer lists
    is missing.

    Args:
        catalog: The loaded ERDDAP `Catalog`.

    Returns:
        `(counts, todo)` — per-bucket counts and the sorted `addressable`
        (griddap) ids not yet curated.

    Raises:
        ValueError: If the `available_datasets:` index is empty.
    """
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    if not available:
        raise ValueError(
            "available_datasets: is empty — run `refresh erddap --write` first"
        )
    curated = set(catalog.datasets)
    structures = _erddap_structures(catalog)
    buckets: dict[str, list[str]] = {}
    for dataset_id in available:
        bucket = _erddap_classify(dataset_id, structures.get(dataset_id), curated)
        buckets.setdefault(bucket, []).append(dataset_id)
    counts = {bucket: len(buckets.get(bucket, [])) for bucket in _COVERAGE_BUCKETS}
    return counts, sorted(buckets.get("addressable", []))


#: Provider id -> a callable returning `(counts, todo)` for `audit --coverage`.
#: Only providers with a discoverable available-universe distinct from their
#: curated rows (gee's STAC index, erddap's `allDatasets` crawl) qualify.
_COVERAGE: dict[str, Callable[[Any], tuple[dict[str, int], list[str]]]] = {
    "gee": _gee_coverage,
    "erddap": _erddap_coverage,
}


def coverage_one(info: BackendInfo) -> CoverageOutcome:
    """Classify a provider's available universe by curation status.

    Powers `audit --coverage`: walks the provider's `available_*` index and
    buckets each id (already curated / worth curating / out of scope / gone).
    Providers without a classifier report `"unsupported"`; fetch failures
    report `"error"` — never raises.

    Args:
        info: The backend to classify.

    Returns:
        The :class:`CoverageOutcome` for `info`.
    """
    classifier = _COVERAGE.get(info.provider)
    if classifier is None:
        return CoverageOutcome(
            provider=info.provider,
            status="unsupported",
            detail="no curation-coverage classifier wired up for this provider",
        )
    try:
        catalog = load_catalog(info)
        counts, todo = classifier(catalog)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return CoverageOutcome(provider=info.provider, status="error", detail=str(exc))
    return CoverageOutcome(
        provider=info.provider, status="ok", counts=counts, todo=todo
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
            As a seatbelt against a transient outage blanking a populated
            index, a write whose live fetch returned **no ids** is refused
            (the skip is reported in `detail`, not treated as an error).

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
        elif not live:
            # Seatbelt: an empty live fetch (transient outage, unexpected body,
            # an SDK returning nothing) must never overwrite a populated bundled
            # index with `[]`. Refuse the write and report it instead.
            detail = (
                "live fetch returned 0 ids; refusing to overwrite the index "
                "(re-run when the source is reachable, or edit by hand)"
            )
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
