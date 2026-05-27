"""Maintain the bundled Overture catalog (`src/earthlens/overture/`).

A single `argparse` subcommand CLI over the official `overturemaps` SDK
(which reads the Overture STAC catalog). Run with no args to see the
subcommand list:

    pixi run -e dev python tools/overture/refresh_overture_catalog.py --help

Subcommands:

* `refresh` — list the available Overture releases (newest first) via
  `overturemaps.core.get_available_releases` and rewrite the
  `available_releases:` block of `overture_data_catalog.yaml` in place,
  preserving the curated `themes:` block and comments. Reloads the
  catalog at the end so a broken rewrite fails the run.
* `validate` — for every curated theme's default type, fetch a tiny
  bbox against a release and confirm the type resolves and carries a
  `sources` column. `--strict` exits 1 on any drift (a curated type the
  live data rejects, or a missing `sources` column).
* `probe <type>` — fetch a tiny bbox for one Overture type and print its
  row count and columns (handy when curating a new theme).

Exits 0 on success, 1 on any HTTP / parse / drift error. Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from earthlens.overture.catalog import CATALOG_PATH, Catalog  # noqa: E402

#: A tiny Times-Square block bbox (W, S, E, N) used by `validate` / `probe`
#: — small enough to fetch in seconds and reliably non-empty.
_SAMPLE_BBOX = (-73.987, 40.757, -73.984, 40.759)

#: Matches the trailing `available_releases:` block (to EOF) so `refresh`
#: can replace only that block and keep the curated `themes:` + comments.
_RELEASES_BLOCK = re.compile(r"^available_releases:.*\Z", re.DOTALL | re.MULTILINE)


def _list_releases() -> list[str]:
    """Return the available Overture release ids, newest first.

    Returns:
        list[str]: Release ids (`"2026-05-20.0"`, …), sorted descending.
    """
    from overturemaps.core import get_available_releases

    releases, _latest = get_available_releases()
    return sorted(set(releases), reverse=True)


def _render_block(releases: list[str]) -> str:
    """Render the `available_releases:` YAML block (no comment preamble).

    The curated comment above the block is left untouched in the YAML; this
    renders only the `available_releases:` key and its items.
    """
    if not releases:
        return "available_releases: []\n"
    lines = ["available_releases:"] + [f"  - {release}" for release in releases]
    return "\n".join(lines) + "\n"


def _refresh(args: argparse.Namespace) -> int:
    """Rewrite the `available_releases:` block in place from the live SDK."""
    releases = _list_releases()
    text = CATALOG_PATH.read_text(encoding="utf-8")
    new_block = _render_block(releases)
    if _RELEASES_BLOCK.search(text):
        text = _RELEASES_BLOCK.sub(new_block.rstrip("\n"), text).rstrip("\n") + "\n"
    else:
        text = text.rstrip("\n") + "\n" + new_block
    CATALOG_PATH.write_text(text, encoding="utf-8")
    reloaded = Catalog.load()
    print(
        f"Wrote {len(releases)} release(s) to {CATALOG_PATH.name}; "
        f"latest={reloaded.latest_release()!r}."
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    """Fetch a tiny sample per curated theme/default-type and report drift."""
    from overturemaps.core import geodataframe

    cat = Catalog()
    drift: list[str] = []
    for name in cat.themes():
        theme = cat.get_theme(name)
        overture_type = theme.default_type
        try:
            gdf = geodataframe(overture_type, bbox=_SAMPLE_BBOX, release=args.release)
        except Exception as exc:  # noqa: BLE001 - reported as drift
            drift.append(f"{name}/{overture_type}: fetch failed ({exc})")
            continue
        has_sources = "sources" in gdf.columns
        if not has_sources:
            drift.append(f"{name}/{overture_type}: no 'sources' column")
        print(f"{name}/{overture_type}: {len(gdf)} rows, sources={has_sources}")
    if drift:
        print("\nDRIFT:")
        for item in drift:
            print(f"  - {item}")
        return 1 if args.strict else 0
    print("\nNo drift: every curated theme/default-type resolves with sources.")
    return 0


def _probe(args: argparse.Namespace) -> int:
    """Fetch a tiny bbox for one type and print its row count + columns."""
    from overturemaps.core import geodataframe

    gdf = geodataframe(args.type, bbox=_SAMPLE_BBOX, release=args.release)
    print(f"{args.type}: {len(gdf)} rows")
    print("columns:", list(gdf.columns))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the subcommand CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="rewrite available_releases from the SDK")
    p_refresh.set_defaults(func=_refresh)

    p_validate = sub.add_parser("validate", help="check curated types against live data")
    p_validate.add_argument("--release", default=None, help="release id (default latest)")
    p_validate.add_argument(
        "--strict", action="store_true", help="exit 1 on any drift"
    )
    p_validate.set_defaults(func=_validate)

    p_probe = sub.add_parser("probe", help="dump one type's row count + columns")
    p_probe.add_argument("type", help="Overture feature type (e.g. place, building)")
    p_probe.add_argument("--release", default=None, help="release id (default latest)")
    p_probe.set_defaults(func=_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to `sys.argv[1:]`).

    Returns:
        int: Process exit code (0 on success).
    """
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
