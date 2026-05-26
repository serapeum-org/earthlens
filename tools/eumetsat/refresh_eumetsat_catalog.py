"""Refresh the EUMETSAT `available_collections` index (C7).

The EUMETSAT analog of `tools/gee/refresh_gee_catalog.py`. Walks the live
Data Store collection list (via `eumdac`) and rebuilds the informational
`available_collections:` index in `catalog/_index.yaml`, and can emit a
curated stanza for a single collection to paste into a per-group file.

Run:

    pixi run -e dev python tools/eumetsat/refresh_eumetsat_catalog.py refresh
    pixi run -e dev python tools/eumetsat/refresh_eumetsat_catalog.py \\
        add-collection msg-hrseviri EO:EUM:DAT:MSG:HRSEVIRI --group MSG

Requires the `eumetsat` extra (`eumdac`) and credentials
(`EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`). Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import sys

import yaml
from _store import (
    INDEX_PATH,
    browse_collection_detail,
    browse_collection_ids,
)

_INDEX_HEADER = (
    "# Informational index of every EUMETSAT Data Store collection id, walked from\n"
    "# the public browse endpoint (api.eumetsat.int/data/browse/collections) by\n"
    "# tools/eumetsat/refresh_eumetsat_catalog.py. Runtime code does not consume\n"
    "# this; it is the full-catalog counterpart to the curated `collections:` rows\n"
    "# in the per-group files (the GEE / Earthdata pattern: available = the whole\n"
    "# provider catalog, curated = the vetted subset).\n"
)


def _write_index(ids: list[str]) -> None:
    """Write the `available_collections` index, preserving the header."""
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        fh.write(_INDEX_HEADER)
        fh.write("available_collections:\n")
        for cid in ids:
            fh.write(f'  - "{cid}"\n')


def refresh() -> int:
    """Rebuild `available_collections` from the public browse endpoint."""
    ids = browse_collection_ids()
    _write_index(ids)
    print(f"wrote {len(ids)} collection ids to {INDEX_PATH}")
    return 0


def add_collection(key: str, collection_id: str, group: str) -> int:
    """Emit a curated catalog stanza for one collection (stdout).

    Reads the collection's public browse metadata (no credentials) to seed
    the stanza title; the maintainer fills in `format` / `selectors` /
    `tailor_product_type` before pasting it into a per-group file.
    """
    detail = browse_collection_detail(collection_id)
    props = (detail.get("collection") or {}).get("properties") or {}
    title = props.get("title") or ""
    stanza = {
        key: {
            "collection_id": collection_id,
            "group": group,
            "output_kind": "raster",
            "format": "",
            "selectors": [],
            "tailor_product_type": None,
        }
    }
    print(f"# {title}")
    print(yaml.safe_dump({"collections": stanza}, sort_keys=False, allow_unicode=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch the refresh / add-collection command."""
    parser = argparse.ArgumentParser(description="Refresh the EUMETSAT catalog index.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="rebuild available_collections from the live store")
    add = sub.add_parser("add-collection", help="emit a curated stanza for one id")
    add.add_argument("key", help="friendly catalog key, e.g. msg-hrseviri")
    add.add_argument("collection_id", help="the real EO:EUM:DAT:... id")
    add.add_argument("--group", default="MSG", help="Data Store group label")
    args = parser.parse_args(argv)
    if args.command == "refresh":
        return refresh()
    return add_collection(args.key, args.collection_id, args.group)


if __name__ == "__main__":
    sys.exit(main())
