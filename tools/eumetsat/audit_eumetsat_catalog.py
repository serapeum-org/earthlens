"""Audit the curated EUMETSAT catalog against the live Data Store (C8).

The EUMETSAT analog of `tools/gee/audit_gee_datasets.py`. Diffs every
curated collection row and the `available_collections` index against the
collection set the live Data Store currently lists, and reports:

* **gone** — a curated `collection_id` the live store no longer lists
  (renamed / retired collection).
* **index-gone** — an `available_collections` id no longer live.
* **new** — a live collection id absent from `available_collections`
  (the index is stale; re-run the refresh tool).

Run:

    pixi run -e dev python tools/eumetsat/audit_eumetsat_catalog.py [--strict]

`--strict` exits non-zero when any drift is found (for CI). Requires the
`eumetsat` extra (`eumdac`) and credentials. Not part of the installed
package.
"""

from __future__ import annotations

import argparse
import sys

from _store import browse_collection_ids, diff_catalog


def audit(strict: bool) -> int:
    """Diff the curated catalog against the live store; return an exit code."""
    from earthlens.eumetsat import Catalog

    catalog = Catalog()
    curated_ids = {c.collection_id for c in catalog.collections.values()}
    available_ids = set(catalog.available_collections)

    # The browse endpoint is public, so the audit needs no credentials.
    live_ids = set(browse_collection_ids())

    findings = diff_catalog(live_ids, curated_ids, available_ids)
    for cid in findings["gone"]:
        print(f"GONE        {cid}: curated row no longer live")
    for cid in findings["index_gone"]:
        print(f"INDEX-GONE  {cid}: available_collections entry no longer live")
    for cid in findings["new"]:
        print(f"NEW         {cid}: live but absent from available_collections")

    total = sum(len(v) for v in findings.values())
    if total == 0:
        print("no drift: catalog matches the live Data Store")
        return 0
    print(f"{total} drift finding(s)")
    return 1 if strict else 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the audit."""
    parser = argparse.ArgumentParser(description="Audit the EUMETSAT catalog vs live.")
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on any drift (CI)"
    )
    args = parser.parse_args(argv)
    return audit(args.strict)


if __name__ == "__main__":
    sys.exit(main())
