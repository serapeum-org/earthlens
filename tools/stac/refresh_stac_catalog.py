"""Maintain the bundled STAC catalog index (`src/earthlens/stac/catalog/_index.yaml`).

A small ``argparse`` CLI that walks each curated endpoint's ``/collections`` and
rewrites the per-endpoint ``available_collections:`` index in place — the direct
analogue of ``tools/gee/refresh_gee_catalog.py`` walking the Earth Engine STAC.
Run with no args to see the subcommand list::

    python tools/stac/refresh_stac_catalog.py --help

Subcommands:

* ``refresh`` — for every endpoint in the catalog's ``endpoints:`` block (or just
  ``--endpoint <key>``), open the STAC API and list every collection it serves,
  then rewrite the ``available_collections:`` mapping in ``_index.yaml``. The
  walk is **anonymous** — listing ``/collections`` needs no asset credentials,
  even for endpoints whose *asset reads* require signing (MPC SAS, CDSE S3).
  ``--dry-run`` prints the regenerated block instead of writing it.

The curated ``collections:`` blocks in the per-endpoint files are **not** touched
— per-collection asset curation is the job of ``probe_stac_assets.py`` (C8) and
hand review. Exits 0 on success, 1 on any HTTP / parse error. Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

CATALOG_INDEX_PATH = Path("src/earthlens/stac/catalog/_index.yaml")


def _list_collection_ids(url: str, *, verbose: bool = False) -> list[str]:
    """Return the sorted collection ids an endpoint serves.

    Opens the STAC API anonymously (collection listing is public on every
    supported endpoint) and walks ``/collections``.

    Args:
        url: STAC API root URL.
        verbose: When `True`, print progress to stderr.

    Returns:
        The endpoint's collection ids, sorted.

    Raises:
        RuntimeError: When the endpoint cannot be reached or listed.
    """
    from pyramids.stac import open_client

    if verbose:
        print(f"  opening {url}", file=sys.stderr)
    client = open_client(url)
    try:
        ids = [c.id for c in client.get_collections()]
    except Exception as exc:  # noqa: BLE001 - re-raise with endpoint context
        raise RuntimeError(f"failed to list collections at {url}: {exc}") from exc
    if verbose:
        print(f"  found {len(ids)} collections", file=sys.stderr)
    return sorted(ids)


def _rewrite_available_collections(text: str, available: dict[str, list[str]]) -> str:
    """Return `text` with its `available_collections:` block replaced.

    Everything up to (but excluding) the top-level `available_collections:` line
    is preserved verbatim — header comments and the `endpoints:` block — and the
    regenerated block is appended. When the source has no such block, the new
    block is appended after a blank line.

    Args:
        text: Current `_index.yaml` contents.
        available: Endpoint key → sorted collection ids.

    Returns:
        The updated YAML text (newline-terminated).

    Examples:
        - The endpoints block survives; the index block is replaced:
            ```python
            >>> import yaml
            >>> text = "endpoints:\\n  e:\\n    url: u\\navailable_collections:\\n  e:\\n    - old\\n"
            >>> out = _rewrite_available_collections(text, {"e": ["a", "b"]})
            >>> yaml.safe_load(out)["available_collections"]
            {'e': ['a', 'b']}
            >>> yaml.safe_load(out)["endpoints"]["e"]["url"]
            'u'

            ```
        - A source with no index block gets one appended:
            ```python
            >>> import yaml
            >>> out = _rewrite_available_collections("endpoints:\\n  e:\\n    url: u\\n", {"e": ["x"]})
            >>> yaml.safe_load(out)["available_collections"]
            {'e': ['x']}

            ```
    """
    block = yaml.safe_dump(
        {"available_collections": available},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("available_collections:"):
            head = "".join(lines[:i]).rstrip("\n")
            return f"{head}\n\n{block}"
    return f"{text.rstrip(chr(10))}\n\n{block}"


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Walk each endpoint's `/collections` and rewrite the index block.

    Args:
        args: Parsed CLI args (`catalog_index`, `endpoint`, `dry_run`, `verbose`).

    Returns:
        Process exit code (0 success, 1 on any endpoint failure).
    """
    from earthlens.stac.catalog import Catalog

    catalog = Catalog()
    endpoints = catalog.endpoints
    if args.endpoint:
        if args.endpoint not in endpoints:
            print(
                f"unknown endpoint {args.endpoint!r}; known: {sorted(endpoints)}",
                file=sys.stderr,
            )
            return 1
        endpoints = {args.endpoint: endpoints[args.endpoint]}

    available = dict(catalog.available_collections)
    failed = False
    for key, endpoint in endpoints.items():
        print(f"refreshing {key} ...", file=sys.stderr)
        try:
            available[key] = _list_collection_ids(endpoint.url, verbose=args.verbose)
        except RuntimeError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            failed = True

    new_text = _rewrite_available_collections(
        args.catalog_index.read_text(encoding="utf-8"),
        available,
    )
    if args.dry_run:
        print(new_text)
    else:
        args.catalog_index.write_text(new_text, encoding="utf-8")
        print(f"wrote {args.catalog_index}", file=sys.stderr)
        Catalog.load(args.catalog_index.parent)  # reload so a broken write fails loud
    return 1 if failed else 0


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

    p_refresh = sub.add_parser("refresh", help="walk /collections + rewrite _index.yaml")
    p_refresh.add_argument(
        "--catalog-index", type=Path, default=CATALOG_INDEX_PATH,
        help="path to catalog/_index.yaml",
    )
    p_refresh.add_argument(
        "--endpoint", help="refresh only this endpoint key (default: all)"
    )
    p_refresh.add_argument(
        "--dry-run", action="store_true",
        help="print the regenerated index instead of writing it",
    )
    p_refresh.add_argument("-v", "--verbose", action="store_true", help="print walk progress")
    p_refresh.set_defaults(func=_cmd_refresh)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
