"""Refresh / validate the WorldPop product catalog against the live REST API.

Two commands, mirroring the GEE / USGS catalog tooling:

* `refresh` — crawl `hub.worldpop.org/rest/data` (aliases → sub-aliases) and
  write an informational `available_products` index (every product alias and
  its live sub-alias ids) so the curated catalog can be cross-checked.
* `validate` — structural checks on the bundled curated catalog (always,
  offline) plus, with `--live`, confirm every curated sub-alias id still
  exists upstream and (with `--check-files`) that a sample GeoTIFF URL
  responds.

Usage:
    python tools/worldpop/refresh_worldpop_catalog.py refresh -o available_products.yaml
    python tools/worldpop/refresh_worldpop_catalog.py validate --live
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

import requests
import yaml

from earthlens.worldpop.catalog import GENERATIONS, Catalog
from earthlens.worldpop.rest import BASE_URL, rest_records

#: All top-level product aliases the hub exposes (verified 2026-05).
KNOWN_ALIASES: tuple[str, ...] = (
    "pop",
    "pop_density",
    "pwd",
    "age_structures",
    "births",
    "pregnancies",
    "dependency_ratios",
    "urban_change",
    "gbsg",
    "dug",
    "future_pop",
    "covariates",
)

Getter = Callable[..., requests.Response]


def _json(url: str, get: Getter) -> dict:
    """GET `url` and return the decoded JSON body."""
    resp = get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def crawl_subaliases(
    alias: str, *, base_url: str = BASE_URL, get: Getter = requests.get
) -> list[str]:
    """Return the live sub-alias ids for one product alias (blanks skipped)."""
    data = _json(f"{base_url}/{alias}", get).get("data", [])
    return [
        str(row.get("alias")).strip() for row in data if str(row.get("alias")).strip()
    ]


def refresh(
    *, base_url: str = BASE_URL, get: Getter = requests.get
) -> dict[str, list[str]]:
    """Crawl the hub and build the `alias -> [sub-alias id, …]` index."""
    index: dict[str, list[str]] = {}
    for alias in KNOWN_ALIASES:
        try:
            index[alias] = crawl_subaliases(alias, base_url=base_url, get=get)
        except requests.HTTPError:
            index[alias] = []
    return index


def validate_structure(catalog: Catalog) -> list[str]:
    """Return structural problems with the curated catalog (offline)."""
    problems: list[str] = []
    for alias, product in catalog.datasets.items():
        if not product.subaliases:
            problems.append(f"{alias}: no sub-aliases")
        if product.demographic and product.kind != "mixed":
            problems.append(f"{alias}: demographic but kind != 'mixed'")
        for sub in product.subaliases:
            if sub.generation not in GENERATIONS:
                problems.append(
                    f"{alias}/{sub.id}: unknown generation {sub.generation!r}"
                )
            try:
                sub.years_set()
            except ValueError:
                problems.append(f"{alias}/{sub.id}: bad years {sub.years!r}")
    return problems


def validate_live(
    catalog: Catalog, *, base_url: str = BASE_URL, get: Getter = requests.get
) -> list[str]:
    """Return curated sub-aliases that no longer exist upstream."""
    problems: list[str] = []
    for alias, product in catalog.datasets.items():
        if alias not in KNOWN_ALIASES:
            problems.append(f"{alias}: not a known top-level alias")
            continue
        live = set(crawl_subaliases(alias, base_url=base_url, get=get))
        for sub in product.subaliases:
            if sub.id not in live:
                problems.append(f"{alias}/{sub.id}: missing upstream")
    return problems


def _build_parser() -> argparse.ArgumentParser:
    """Return the argparse CLI for the refresh tool."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    refresh_cmd = sub.add_parser("refresh", help="crawl the hub and write the index")
    refresh_cmd.add_argument(
        "-o", "--out", help="write the index YAML here (else stdout)"
    )
    validate_cmd = sub.add_parser("validate", help="check the curated catalog")
    validate_cmd.add_argument("--live", action="store_true", help="also check upstream")
    validate_cmd.add_argument(
        "--check-files", action="store_true", help="HEAD a sample GeoTIFF per product"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the refresh / validate CLI; return a process exit code."""
    args = _build_parser().parse_args(argv)
    if args.command == "refresh":
        index = refresh()
        text = yaml.safe_dump({"available_products": index}, sort_keys=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"wrote {args.out}")
        else:
            print(text)
        return 0

    catalog = Catalog()
    problems = validate_structure(catalog)
    if args.live:
        problems += validate_live(catalog)
    if args.check_files:
        for alias, product in catalog.datasets.items():
            sub = product.subaliases[0]
            try:
                rest_records(alias, sub.id, "COM")
            except requests.HTTPError:
                problems.append(f"{alias}/{sub.id}: sample query failed")
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    print(f"{len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
