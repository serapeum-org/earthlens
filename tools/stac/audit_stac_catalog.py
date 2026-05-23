"""Audit the curated STAC catalog against what the endpoints actually serve.

Walks each endpoint's live ``/collections`` and flags drift between the curated
catalog and reality: curated collections (resolved through their per-endpoint
aliases) that the endpoint no longer serves, and — informationally — live
collections absent from the ``available_collections`` index. It is the STAC
analogue of ``tools/gee/audit_gee_datasets.py``.

    python tools/stac/audit_stac_catalog.py audit
    python tools/stac/audit_stac_catalog.py audit --strict   # exit 1 on any drift

``--strict`` makes any curated-but-missing collection a non-zero exit, so CI can
fail when an endpoint renames or drops a collection an alias still points at.
Without ``--strict`` the report prints and the command exits 0. Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import sys


def _diff_collections(
    curated: dict[str, set[str]], live: dict[str, set[str]]
) -> dict[str, dict[str, list[str]]]:
    """Return per-endpoint drift between curated and live collection ids.

    Args:
        curated: Endpoint key → set of curated collection ids resolved for that
            endpoint (via aliases).
        live: Endpoint key → set of collection ids the endpoint serves now.

    Returns:
        `{endpoint: {"missing": [...], "untracked": [...]}}`, only for endpoints
        with drift. `missing` = curated ids the endpoint no longer serves;
        `untracked` = live ids not in the curated set (informational).

    Examples:
        - Curated-but-not-live shows as `missing`, live-but-not-curated as `untracked`:
            ```python
            >>> _diff_collections({"e": {"a", "b"}}, {"e": {"b", "c"}})
            {'e': {'missing': ['a'], 'untracked': ['c']}}

            ```
        - An endpoint fully in sync produces no entry:
            ```python
            >>> _diff_collections({"e": {"a"}}, {"e": {"a"}})
            {}

            ```
    """
    report: dict[str, dict[str, list[str]]] = {}
    for endpoint, curated_ids in curated.items():
        live_ids = live.get(endpoint, set())
        missing = sorted(curated_ids - live_ids)
        untracked = sorted(live_ids - curated_ids)
        if missing or untracked:
            entry: dict[str, list[str]] = {}
            if missing:
                entry["missing"] = missing
            if untracked:
                entry["untracked"] = untracked
            report[endpoint] = entry
    return report


def _curated_resolved(catalog: object) -> dict[str, set[str]]:
    """Map each endpoint to the resolved ids of the curated collections it serves.

    A curated collection is "served" by its home endpoint and by any endpoint it
    declares an alias for; each is resolved to the id that endpoint uses.

    Args:
        catalog: A loaded `earthlens.stac.Catalog`.

    Returns:
        Endpoint key → set of resolved curated collection ids.
    """
    out: dict[str, set[str]] = {ep: set() for ep in catalog.endpoints}
    for key, collection in catalog.datasets.items():
        endpoints = {collection.endpoint, *collection.aliases.keys()}
        for endpoint in endpoints:
            if endpoint in out:
                out[endpoint].add(catalog.resolve(endpoint, key))
    return out


def _cmd_audit(args: argparse.Namespace) -> int:
    """Diff the curated catalog against live `/collections` for every endpoint.

    Args:
        args: Parsed CLI args (`strict`).

    Returns:
        Process exit code (1 on a walk failure, or on drift when `--strict`).
    """
    from earthlens.stac.catalog import Catalog
    from pyramids.stac import open_client

    catalog = Catalog()
    curated = _curated_resolved(catalog)

    live: dict[str, set[str]] = {}
    failed = False
    for key, endpoint in catalog.endpoints.items():
        try:
            client = open_client(endpoint.url)
            live[key] = {c.id for c in client.get_collections()}
        except Exception as exc:  # noqa: BLE001 - report + continue
            print(f"ERROR walking {key} ({endpoint.url}): {exc}", file=sys.stderr)
            failed = True

    report = _diff_collections(curated, {k: v for k, v in live.items()})
    if not report:
        print("catalog is in sync with every reachable endpoint.")
    for endpoint, entry in report.items():
        if entry.get("missing"):
            print(f"{endpoint}: MISSING (curated but not served): {entry['missing']}")
        if entry.get("untracked"):
            print(f"{endpoint}: untracked (served but not curated): {len(entry['untracked'])}")

    has_missing = any("missing" in e for e in report.values())
    if failed:
        return 1
    if args.strict and has_missing:
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
    p_audit = sub.add_parser("audit", help="diff curated catalog vs live /collections")
    p_audit.add_argument(
        "--strict", action="store_true",
        help="exit 1 when a curated collection is no longer served",
    )
    p_audit.set_defaults(func=_cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
