"""Audit the curated Sentinel Hub catalog against what the SDK actually serves.

Compares the curated `collections:` / `recipes:` blocks against the authoritative
`sentinelhub.DataCollection` enum and the bundled evalscript `.js` files, flagging
drift. The Sentinel Hub analogue of `tools/openeo/audit_openeo_datasets.py` /
`tools/gee/audit_gee_datasets.py`.

    python tools/sentinel_hub/audit_sh_datasets.py audit
    python tools/sentinel_hub/audit_sh_datasets.py audit --strict   # 1 on drift

It checks four things:

* **curated collections** whose `sh_collection` is not a real `DataCollection`
  member (a hard error under `--strict`);
* **recipe base collections** that are not real `DataCollection` members (hard
  error under `--strict`);
* **recipe evalscripts** that are missing, not `//VERSION=3`, or (for stats
  recipes) lack a `dataMask` band (hard error under `--strict`);
* **untracked** `DataCollection` members absent from both the curated
  collections and the `available_collections` index (informational).

Enumerating the enum is **offline** (no credentials). Without `--strict` the
report prints and the command exits 0. Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys


def _enum_members() -> set[str]:
    """Return the set of `sentinelhub.DataCollection` member names (offline).

    Returns:
        The authoritative set of collection names the SDK can address.
    """
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    sentinelhub = import_sentinelhub()
    return {member.name for member in sentinelhub.DataCollection}


def audit(strict: bool = False) -> int:
    """Run the catalog-vs-SDK audit and print a report.

    Args:
        strict: When `True`, return exit code 1 if any hard drift is found.

    Returns:
        Process exit code (0 clean / non-strict, 1 on strict drift).
    """
    from earthlens.sentinel_hub import Catalog, read_evalscript

    catalog = Catalog()
    members = _enum_members()
    index = set(catalog.available_collections)
    hard = 0

    bad_collections = [
        key for key, col in catalog.datasets.items() if col.sh_collection not in members
    ]
    bad_recipe_collections = [
        key
        for key, rec in catalog.recipes.items()
        if rec.base_collection not in members
    ]
    recipe_problems: list[str] = []
    for key, recipe in catalog.recipes.items():
        try:
            script = read_evalscript(recipe.evalscript)
        except FileNotFoundError:
            recipe_problems.append(f"{key}: missing evalscript {recipe.evalscript}")
            continue
        if script.splitlines()[0].strip() != "//VERSION=3":
            recipe_problems.append(f"{key}: {recipe.evalscript} not //VERSION=3")
        if recipe.kind == "stats" and "dataMask" not in script:
            recipe_problems.append(f"{key}: {recipe.evalscript} stats w/o dataMask")

    curated_collections = {col.sh_collection for col in catalog.datasets.values()}
    untracked = sorted(members - curated_collections - index)

    for label, rows in (
        ("curated collections not in the DataCollection enum", bad_collections),
        ("recipe base collections not in the enum", bad_recipe_collections),
        ("recipe evalscript problems", recipe_problems),
    ):
        if rows:
            hard += len(rows)
            sys.stderr.write(f"[drift] {label}:\n")
            for row in rows:
                sys.stderr.write(f"  - {row}\n")

    if untracked:
        sys.stderr.write(
            f"[info] {len(untracked)} DataCollection member(s) not curated and not "
            f"in available_collections: {untracked}\n"
        )
    if hard == 0:
        sys.stderr.write("audit: no curated-vs-SDK drift\n")
    if strict and hard:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    audit_cmd = sub.add_parser("audit", help="audit the curated catalog vs the SDK")
    audit_cmd.add_argument(
        "--strict", action="store_true", help="exit 1 on any hard drift"
    )
    audit_cmd.set_defaults(func=lambda args: audit(strict=args.strict))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to `sys.argv`).

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
