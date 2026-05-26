"""Shared helpers for the EUMETSAT catalog tools (refresh / audit).

Builds an `eumdac.DataStore` from `EUMETSAT_CONSUMER_KEY` /
`EUMETSAT_CONSUMER_SECRET` (or `~/.eumdac/credentials`) and exposes the
pure diff logic the audit tool runs, so that logic can be unit-tested
without a network call. Not part of the installed package.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Repo `src/` so the tools can import `earthlens.eumetsat` when run from a
#: checkout that has not `pip install`-ed the package.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

CATALOG_DIR = _SRC / "earthlens" / "eumetsat" / "catalog"
INDEX_PATH = CATALOG_DIR / "_index.yaml"


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
