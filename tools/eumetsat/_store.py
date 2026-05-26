"""Shared helpers for the EUMETSAT catalog tools (refresh / audit / probe).

The Data Store **browse** endpoint is public (no token), so the catalog
tools walk it directly with `requests` — no credentials needed. An
`eumdac.DataStore` (which does need credentials) is only built for
operations that actually search/fetch products. The pure diff logic the
audit tool runs lives here too so it can be unit-tested without a network
call. Not part of the installed package.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

#: Repo `src/` so the tools can import `earthlens.eumetsat` when run from a
#: checkout that has not `pip install`-ed the package.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CATALOG_DIR = _SRC / "earthlens" / "eumetsat" / "catalog"
INDEX_PATH = CATALOG_DIR / "_index.yaml"

#: Public Data Store browse endpoints (no authentication required).
BROWSE_URL = "https://api.eumetsat.int/data/browse/collections"
BROWSE_DETAIL_URL = "https://api.eumetsat.int/data/browse/1.0.0/collections/{cid}"


def browse_collection_ids(timeout: float = 30.0) -> list[str]:
    """Return every collection id from the public browse endpoint (sorted).

    Walks `api.eumetsat.int/data/browse/collections` (public, no token) and
    extracts the `EO:EUM:DAT:…` id from each link.

    Args:
        timeout: Per-request timeout in seconds.

    Returns:
        list[str]: The collection-id strings the Data Store lists, sorted.
    """
    resp = requests.get(BROWSE_URL, params={"format": "json"}, timeout=timeout)
    resp.raise_for_status()
    links = resp.json().get("links") or []
    return sorted({link["title"] for link in links if link.get("title")})


def browse_collection_detail(collection_id: str, timeout: float = 30.0) -> dict:
    """Return the public browse metadata for one collection.

    Args:
        collection_id: A Data Store `EO:EUM:DAT:…` id.
        timeout: Per-request timeout in seconds.

    Returns:
        dict: The parsed JSON metadata document for the collection.
    """
    from urllib.parse import quote

    url = BROWSE_DETAIL_URL.format(cid=quote(collection_id, safe=""))
    resp = requests.get(url, params={"format": "json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def build_datastore():
    """Return a live `eumdac.DataStore`, or exit with a friendly message.

    Resolves credentials through `EumetsatAuth` (env → kwargs →
    `~/.eumdac/credentials`) and mints a token. Exits the process (not
    raises) when `eumdac` or the credentials are missing, so the CLI
    degrades gracefully.

    Returns:
        eumdac.DataStore: A Data Store client bound to a fresh token.
    """
    try:
        import eumdac  # noqa: F401
    except ImportError:
        sys.exit(
            "eumdac is required for this tool; install "
            "`pip install earthlens[eumetsat]`."
        )
    from earthlens.base import AuthenticationError
    from earthlens.eumetsat import EumetsatAuth, EumetsatCredentials

    auth = EumetsatAuth(EumetsatCredentials())
    try:
        auth.configure()
    except AuthenticationError as exc:
        sys.exit(str(exc))
    return auth.datastore()


def live_collection_ids(store) -> list[str]:
    """Return every collection id the Data Store currently lists.

    Args:
        store: A live `eumdac.DataStore`.

    Returns:
        list[str]: The collection-id strings, sorted.
    """
    return sorted(str(c) for c in store.collections)


def diff_catalog(
    live_ids: set[str],
    curated_ids: set[str],
    available_ids: set[str],
) -> dict[str, list[str]]:
    """Diff the curated catalog and index against the live collection set.

    Pure function (no I/O) so it is unit-testable. Reports:

    * `gone` — a curated `collection_id` the live store no longer lists.
    * `index_gone` — an `available_collections` id no longer live.
    * `new` — a live id absent from `available_collections`.

    Args:
        live_ids: Collection ids the live Data Store lists.
        curated_ids: `collection_id`s of the curated rows.
        available_ids: ids in the `available_collections` index.

    Returns:
        dict[str, list[str]]: Sorted finding lists keyed by category.
    """
    return {
        "gone": sorted(curated_ids - live_ids),
        "index_gone": sorted(available_ids - live_ids),
        "new": sorted(live_ids - available_ids),
    }
