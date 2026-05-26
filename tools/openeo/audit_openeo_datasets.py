"""Audit the curated openEO catalog against what the backend actually serves.

Lists the backend's live collections + processes and flags drift between the
curated catalog and reality. The openEO analogue of
`tools/stac/audit_stac_catalog.py` / `tools/gee/audit_gee_datasets.py`.

    python tools/openeo/audit_openeo_datasets.py audit
    python tools/openeo/audit_openeo_datasets.py audit --strict   # exit 1 on any drift

It checks three things:

* **curated collections** whose `collection_id` the backend no longer serves
  (a hard error under `--strict`);
* **recipe processes** the backend no longer advertises (hard error under
  `--strict`);
* **untracked** live collections absent from the curated catalog and from the
  `available_collections` index (informational).

Listing is **anonymous** (no OIDC login). Without `--strict` the report prints
and the command exits 0. Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_ENDPOINT = "https://openeo.dataspace.copernicus.eu"


def _live_ids(endpoint: str, kind: str) -> set[str]:
    """Return the set of live collection/process ids from the backend.

    Args:
        endpoint: openEO API root URL.
        kind: `"collections"` or `"processes"`.

    Returns:
        The live id set.

    Raises:
        RuntimeError: When the backend cannot be reached or listed.
    """
    import openeo

    try:
        connection = openeo.connect(endpoint)
        lister = getattr(connection, f"list_{kind}")
        return {entry["id"] for entry in lister()}
    except Exception as exc:  # noqa: BLE001 - re-raise with context
        raise RuntimeError(f"failed to list {kind} at {endpoint}: {exc}") from exc


def _cmd_audit(args: argparse.Namespace) -> int:
    """Diff the curated catalog against the live backend.

    Args:
        args: Parsed CLI args (`endpoint`, `strict`).

    Returns:
        Process exit code (1 on a listing failure, or on drift when `--strict`).
    """
    from earthlens.openeo.catalog import Catalog

    catalog = Catalog()
    try:
        live_collections = _live_ids(args.endpoint, "collections")
        live_processes = _live_ids(args.endpoint, "processes")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    curated_ids = {col.collection_id for col in catalog.datasets.values()}
    missing_collections = sorted(curated_ids - live_collections)

    missing_processes: dict[str, list[str]] = {}
    for key, recipe in catalog.recipes.items():
        gone = [
            next(iter(step))
            for step in recipe.graph
            if next(iter(step)) not in live_processes
        ]
        if recipe.base_collection not in live_collections:
            gone.append(f"<base {recipe.base_collection}>")
        if gone:
            missing_processes[key] = gone

    untracked = sorted(
        live_collections - curated_ids - set(catalog.available_collections)
    )

    print(f"openEO catalog audit against {args.endpoint}")
    print(f"  curated collections: {len(curated_ids)} / live {len(live_collections)}")
    print(f"  curated recipes:     {len(catalog.recipes)}")
    if missing_collections:
        print("  MISSING collections (curated but not served):")
        for cid in missing_collections:
            print(f"    - {cid}")
    if missing_processes:
        print("  MISSING recipe processes/collections (curated but not advertised):")
        for key, gone in missing_processes.items():
            print(f"    - {key}: {', '.join(gone)}")
    if untracked:
        print("  untracked live collections (not curated, not in index):")
        for cid in untracked:
            print(f"    - {cid}")
    if not (missing_collections or missing_processes or untracked):
        print("  no drift: curated catalog matches the live backend.")

    drift = bool(missing_collections or missing_processes)
    if drift and args.strict:
        print("FAIL: drift detected (--strict).", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to `sys.argv[1:]`).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_audit = sub.add_parser("audit", help="diff curated catalog vs live backend")
    p_audit.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="openEO API root URL"
    )
    p_audit.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a curated collection/process is no longer served",
    )
    p_audit.set_defaults(func=_cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
