"""Maintain the bundled USGS Water parameter catalog.

A single `argparse` subcommand CLI that refreshes the informational
parameter-code index from the live USGS reference table, appends curated
rows, and validates the curated catalog. Run with no args for the full
subcommand list:

    pixi run -e dev python tools/usgs_water/refresh_usgs_catalog.py --help

Subcommands:

* `refresh` — pull the USGS parameter-code reference table via
  `dataretrieval.waterdata.get_reference_table(collection='parameter-codes')`
  (the modern replacement for the removed `nwis.get_pmcodes`) and rewrite
  the informational `available_parameters.yaml` index next to the curated
  catalog. The full ~25k-code table is *not* hand-curated; this index lets
  the docs and the `validate` step see every real code.
* `add-parameter <key> <code>` — append a curated stanza (friendly name
  -> code, with `--name` / `--units` / `--group` / `--services`) to
  `usgs_water_data_catalog.yaml`, then reload the catalog so a broken
  stanza fails the run.
* `validate` — assert every curated row's `services` entries are known
  `service=` values and (when the index exists) every curated code is a
  member of the refreshed `available_parameters` index.

Exits 0 on success, 1 on any HTTP / parse / validation error.
Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from earthlens.usgs_water import _helpers
from earthlens.usgs_water.backend import SERVICES
from earthlens.usgs_water.catalog import CATALOG_PATH, Catalog

#: Informational, refreshed index of every real USGS parameter code.
AVAILABLE_PATH: Path = CATALOG_PATH.parent / "available_parameters.yaml"

#: USGS reference-table collection holding the parameter-code list.
_REFERENCE_COLLECTION = "parameter-codes"


def _import_reference_table():
    """Return the modern `get_reference_table` callable (lazy import)."""
    try:
        import dataretrieval.waterdata as waterdata
    except ImportError as exc:  # pragma: no cover - tool runs with the extra
        raise SystemExit(
            "refresh needs the 'dataretrieval' SDK: pip install "
            "earthlens[usgs-water]"
        ) from exc
    return waterdata.get_reference_table


def refresh(_args: argparse.Namespace) -> int:
    """Rewrite `available_parameters.yaml` from the live reference table."""
    get_reference_table = _import_reference_table()
    try:
        result = get_reference_table(collection=_REFERENCE_COLLECTION)
    except Exception as exc:  # noqa: BLE001 - surfaced as a friendly message
        if _helpers.is_rate_limit_error(exc):
            raise SystemExit(
                "The modern USGS reference-table endpoint rate-limited this "
                "anonymous request (HTTP 429). Set API_USGS_PAT (or pass a "
                "token) and retry: the index needs one un-throttled call."
            ) from exc
        raise
    frame = result[0] if isinstance(result, tuple) else result
    index: dict[str, dict[str, str]] = {}
    for _, row in frame.iterrows():
        code = str(
            row.get("parameter_code")
            or row.get("parameterCode")
            or row.get("id")
            or ""
        ).strip()
        if not code:
            continue
        index[code] = {
            "name": str(row.get("parameter_name") or row.get("name") or ""),
            "group": str(row.get("parameter_group_code") or row.get("group") or ""),
            "unit": str(row.get("unit_of_measure") or row.get("unit") or ""),
        }
    AVAILABLE_PATH.write_text(
        yaml.safe_dump(
            {"available_parameters": dict(sorted(index.items()))},
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(index)} parameter codes to {AVAILABLE_PATH}")
    return 0


def add_parameter(args: argparse.Namespace) -> int:
    """Append a curated parameter stanza and reload the catalog."""
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    params = data.setdefault("parameters", {})
    if args.key in params:
        print(f"{args.key!r} is already curated; edit the YAML by hand.")
        return 1
    params[args.key] = {
        "code": args.code,
        "name": args.name or args.key.replace("_", " ").title(),
        "units": args.units or "",
        "group": args.group,
        "services": args.services or ["daily", "instantaneous"],
    }
    CATALOG_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    Catalog.load()  # fail loudly if the new stanza is invalid
    print(f"added {args.key!r} -> {args.code}")
    return 0


def validate(_args: argparse.Namespace) -> int:
    """Check every curated row's services + code membership in the index."""
    catalog = Catalog.load()
    errors: list[str] = []
    for name, param in catalog.parameters.items():
        for service in param.services:
            if service not in SERVICES:
                errors.append(f"{name!r}: unknown service {service!r}")
    if AVAILABLE_PATH.exists():
        index = (
            yaml.safe_load(AVAILABLE_PATH.read_text(encoding="utf-8")) or {}
        ).get("available_parameters", {})
        for name, param in catalog.parameters.items():
            if index and param.code not in index:
                errors.append(f"{name!r}: code {param.code} not in the live index")
    else:
        print("note: available_parameters.yaml not found; run refresh for code checks.")
    if errors:
        print("validation FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"validation OK: {len(catalog.parameters)} curated parameters.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the subcommand CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("refresh", help="rebuild available_parameters.yaml").set_defaults(
        func=refresh
    )

    add = sub.add_parser("add-parameter", help="append a curated parameter row")
    add.add_argument("key", help="friendly catalog key (e.g. 'discharge')")
    add.add_argument("code", help="5-digit NWIS parameter code (e.g. '00060')")
    add.add_argument("--name", default="", help="human-readable name")
    add.add_argument("--units", default="", help="reporting units")
    add.add_argument("--group", default="Physical", help="USGS parameter group")
    add.add_argument(
        "--services", nargs="*", default=None, help="services the code is valid on"
    )
    add.set_defaults(func=add_parameter)

    sub.add_parser("validate", help="validate the curated catalog").set_defaults(
        func=validate
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the chosen subcommand."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
