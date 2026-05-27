"""Maintain the bundled Sentinel Hub catalog index + validate evalscript recipes.

A small `argparse` CLI — the analogue of `tools/openeo/refresh_openeo_catalog.py`
and `tools/gee/refresh_gee_catalog.py` — that refreshes the informational
`available_collections:` index from the live Catalog API and validates curated
evalscript recipes against the bundled `.js` files.

Run with no args to see the subcommand list:

    python tools/sentinel_hub/refresh_sh_catalog.py --help

Subcommands:

* `refresh --from-sdk` — **(preferred, offline, no credentials)** enumerate the
  `sentinelhub.DataCollection` enum and rewrite the `available_collections:` list
  in `_index.yaml`. These are the UPPERCASE names you pass to the backend via a
  collection's `sh_collection` (e.g. `SENTINEL2_L2A`), so they line up with the
  curated catalog.
* `refresh` — authenticate (OAuth2 client-credentials from `SENTINELHUB_CLIENT_ID`
  / `SENTINELHUB_CLIENT_SECRET`), call `SentinelHubCatalog.get_collections()`, and
  rewrite the list. **Caveat:** the live Catalog API returns STAC-style ids
  (lowercase/hyphenated, e.g. `sentinel-2-l2a`) in a *different* namespace from
  the `DataCollection` enum names the backend uses — so prefer `--from-sdk` for
  the index unless you specifically want the live STAC ids. `--dry-run` prints
  the regenerated file instead of writing it.
* `validate-recipe <key>` — check the recipe resolves, its bundled `.js` exists,
  starts with `//VERSION=3`, and (for `kind="stats"`) declares a `dataMask`
  band; exit non-zero on any drift. No network.
* `validate-all` — run `validate-recipe` over every curated recipe.

The curated `collections:` / `recipes:` blocks are not touched — per-row
curation is hand review. Exits 0 on success, 1 on any error. Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

CATALOG_INDEX_PATH = Path("src/earthlens/sentinel_hub/catalog/_index.yaml")


def _collection_ids(from_sdk: bool, endpoint: str | None) -> tuple[list[str], str]:
    """Resolve the available-collection ids + a provenance label.

    Args:
        from_sdk: When `True`, enumerate the `sentinelhub.DataCollection` enum
            **offline** (no credentials). Otherwise query the live Catalog API.
        endpoint: Endpoint alias / URL for the live query (ignored offline).

    Returns:
        `(sorted_ids, source_label)`.
    """
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    sentinelhub = import_sentinelhub()
    if from_sdk:
        ids = sorted(member.name for member in sentinelhub.DataCollection)
        return ids, "the sentinelhub DataCollection enum (offline)"
    from earthlens.sentinel_hub.auth import SentinelHubAuth

    config = SentinelHubAuth(endpoint=endpoint).config()
    catalog = sentinelhub.SentinelHubCatalog(config=config)
    ids = sorted({c.get("id") for c in catalog.get_collections() if c.get("id")})
    return ids, "the live Catalog API"


def _refresh(args: argparse.Namespace) -> int:
    """Rebuild `available_collections:` from the SDK enum or the live Catalog API.

    Args:
        args: Parsed CLI arguments (`endpoint`, `dry_run`, `from_sdk`).

    Returns:
        Process exit code (0 success, 1 on error).
    """
    ids, source = _collection_ids(args.from_sdk, args.endpoint)
    text = (
        "# Informational index of the Sentinel Hub data collections the backend\n"
        "# can address. Rebuilt by tools/sentinel_hub/refresh_sh_catalog.py from\n"
        f"# {source} on {dt.date.today().isoformat()}.\n"
        + yaml.safe_dump({"available_collections": ids}, sort_keys=False)
    )
    if args.dry_run:
        sys.stdout.write(text)
        return 0
    CATALOG_INDEX_PATH.write_text(text, encoding="utf-8")
    sys.stderr.write(f"wrote {len(ids)} collections to {CATALOG_INDEX_PATH}\n")
    return 0


def _validate_one(key: str) -> list[str]:
    """Validate one recipe; return a list of problem strings (empty = ok).

    Args:
        key: A curated recipe key.

    Returns:
        Human-readable problems found (empty when the recipe is valid).
    """
    from earthlens.sentinel_hub import Catalog, read_evalscript

    catalog = Catalog()
    problems: list[str] = []
    try:
        recipe = catalog.get_recipe(key)
    except ValueError as exc:
        return [str(exc)]
    try:
        script = read_evalscript(recipe.evalscript)
    except FileNotFoundError as exc:
        return [str(exc)]
    if script.splitlines()[0].strip() != "//VERSION=3":
        problems.append(f"{recipe.evalscript} does not start with //VERSION=3")
    if recipe.kind == "stats" and "dataMask" not in script:
        problems.append(
            f"{recipe.evalscript} is a stats recipe but declares no dataMask band"
        )
    return problems


def _validate_recipe(args: argparse.Namespace) -> int:
    """Validate a single recipe key.

    Args:
        args: Parsed CLI arguments (`key`).

    Returns:
        Process exit code.
    """
    problems = _validate_one(args.key)
    if problems:
        for problem in problems:
            sys.stderr.write(f"{args.key}: {problem}\n")
        return 1
    sys.stderr.write(f"{args.key}: ok\n")
    return 0


def _validate_all(args: argparse.Namespace) -> int:
    """Validate every curated recipe.

    Args:
        args: Parsed CLI arguments (unused).

    Returns:
        Process exit code (1 if any recipe is invalid).
    """
    from earthlens.sentinel_hub import Catalog

    failed = False
    for key in sorted(Catalog().recipes):
        problems = _validate_one(key)
        if problems:
            failed = True
            for problem in problems:
                sys.stderr.write(f"{key}: {problem}\n")
        else:
            sys.stderr.write(f"{key}: ok\n")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser(
        "refresh", help="rebuild available_collections from the Catalog API"
    )
    refresh.add_argument("--endpoint", default=None, help="endpoint alias or base URL")
    refresh.add_argument(
        "--dry-run", action="store_true", help="print instead of writing"
    )
    refresh.add_argument(
        "--from-sdk",
        action="store_true",
        help="enumerate the DataCollection enum offline (no credentials)",
    )
    refresh.set_defaults(func=_refresh)

    validate = sub.add_parser("validate-recipe", help="validate one curated recipe")
    validate.add_argument("key", help="the recipe key")
    validate.set_defaults(func=_validate_recipe)

    validate_all = sub.add_parser("validate-all", help="validate every curated recipe")
    validate_all.set_defaults(func=_validate_all)
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
