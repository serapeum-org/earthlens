"""Maintain the bundled HDX data catalog (`src/earthlens/hdx/catalog/`).

A single `argparse` subcommand CLI over the read-only `hdx-python-api`
client. Run with no args to see the subcommand list:

    pixi run -e dev python tools/hdx/refresh_hdx_catalog.py --help

Subcommands:

* `refresh` — run `Dataset.search_in_hdx(...)` filtered by organisation
  / tag and rewrite the informational `available_datasets:` index in
  `catalog/_available.json`. This is **not** the full ~21k HDX catalogue —
  it is the curated orgs' / tags' datasets, the analogue of
  `tools/gee/refresh_gee_catalog.py`'s `available_datasets:` block.
* `add-dataset <key> <hdx_id>` — fetch one dataset's live metadata and
  print a ready-to-paste curated `datasets:` stanza, inferring
  `formats` (CKAN labels) and `output_kinds` from its resources.
* `audit` — read the bundled catalog and confirm every curated
  `hdx_id` still resolves live via `Dataset.read_from_hdx`. With
  `--strict`, exit 1 on any catalog-vs-live drift.

Exits 0 on success, 1 on an HTTP / parse error or (under `--strict`) on
drift. Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

#: Map a CKAN format label (lower-cased) to a pyramids output kind. Used
#: to seed the `output_kinds:` field of an `add-dataset` stanza.
_VECTOR_FORMATS = {"geopackage", "shp", "geojson", "kml", "geodatabase", "topojson"}
_RASTER_FORMATS = {"geotiff", "cog", "netcdf", "grib", "img", "ascii grid"}
_TABULAR_FORMATS = {"csv", "xlsx", "xls", "json", "tsv", "parquet"}

INDEX_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "earthlens"
    / "hdx"
    / "catalog"
    / "_available.json"
)


def configure() -> None:
    """Create the read-only HDX configuration once (guarded)."""
    from hdx.api.configuration import Configuration, ConfigurationError

    try:
        Configuration.read()
    except ConfigurationError:
        Configuration.create(
            hdx_site="prod", user_agent="earthlens", hdx_read_only=True
        )


def kind_for_format(fmt: str) -> str | None:
    """Return the pyramids output kind for a CKAN format label.

    Args:
        fmt: A CKAN format label, e.g. `"Geopackage"`, `"CSV"`,
            `"GeoTIFF"`.

    Returns:
        `"vector"`, `"raster"`, `"tabular"`, or `None` when the label is
            not recognised.
    """
    token = fmt.strip().lower()
    if token in _VECTOR_FORMATS:
        return "vector"
    if token in _RASTER_FORMATS:
        return "raster"
    if token in _TABULAR_FORMATS:
        return "tabular"
    return None


def search_datasets(
    query: str = "*:*",
    org: str | None = None,
    tag: str | None = None,
    rows: int = 1000,
    with_formats: bool = False,
) -> list[dict]:
    """Search HDX and return lightweight rows for the matching datasets.

    Args:
        query: CKAN free-text query (default `"*:*"`, every dataset).
        org: Optional organisation slug to filter on (CKAN `fq`).
        tag: Optional tag/theme to filter on (CKAN `fq`).
        rows: Maximum number of datasets to return (`page_size`).
        with_formats: When `True`, also fetch each dataset's resources to
            populate `formats` (one extra request per dataset — slow).
            The `refresh` index does not need it, so it defaults to
            `False`.

    Returns:
        list[dict]: One `{key, hdx_id, org, title, formats}` row per
            matching dataset (`formats` empty unless `with_formats`).
    """
    from hdx.data.dataset import Dataset

    filters = []
    if org:
        filters.append(f"organization:{org}")
    if tag:
        filters.append(f"tags:{tag}")
    fq = " AND ".join(filters) if filters else None
    datasets = Dataset.search_in_hdx(query, fq=fq, page_size=rows)
    rows_out: list[dict] = []
    for dataset in datasets:
        if with_formats:
            try:
                org_name = dataset.get_organization().get("name")
            except Exception:  # noqa: BLE001 - org lookup is best-effort metadata
                org_name = ""
            formats = sorted({r["format"] for r in dataset.get_resources()})
        else:
            org_name = ""
            formats = []
        rows_out.append(
            {
                "key": dataset["name"],
                "hdx_id": dataset["name"],
                "org": org_name,
                "title": dataset.get("title") or "",
                "formats": formats,
            }
        )
    return rows_out


def write_index(names: list[str], index_path: Path = INDEX_PATH) -> int:
    """Rewrite the `available_datasets` JSON index, sorted and de-duped.

    The index is JSON (`_available.json`), kept out of the curated
    `*.yaml` glob so `Catalog()` parses only the small curated YAMLs and
    reads this flat id list separately (the `earthlens.earthdata`
    `_auto.json` pattern).

    Args:
        names: HDX dataset ids to record.
        index_path: Path to `catalog/_available.json`.

    Returns:
        int: The number of unique ids written.
    """
    import json

    unique = sorted(set(names))
    payload = {
        "__comment__": (
            "AUTO-GENERATED by tools/hdx/refresh_hdx_catalog.py. Informational "
            "index of HDX dataset ids for the curated organisations; NOT the "
            "full ~21k catalogue and not consumed at runtime. Kept as JSON (out "
            "of the curated *.yaml glob) so Catalog() stays fast."
        ),
        "available_datasets": unique,
    }
    index_path.write_text(
        json.dumps(payload, indent=0, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return len(unique)


def dataset_stanza(key: str, hdx_id: str) -> str:
    """Build a curated `datasets:` YAML stanza from a live dataset.

    Args:
        key: The friendly catalog key to assign.
        hdx_id: The HDX dataset id to read.

    Returns:
        str: A YAML stanza (one `datasets:` entry) ready to paste into a
            per-theme catalog file.

    Raises:
        ValueError: When `hdx_id` is not found on HDX.
    """
    from hdx.data.dataset import Dataset

    dataset = Dataset.read_from_hdx(hdx_id)
    if dataset is None:
        raise ValueError(f"HDX dataset {hdx_id!r} not found.")
    try:
        org_name = dataset.get_organization().get("name")
    except Exception:  # noqa: BLE001 - org lookup is best-effort metadata
        org_name = ""
    formats = sorted({r["format"] for r in dataset.get_resources()})
    kinds = sorted({k for k in (kind_for_format(f) for f in formats) if k})
    lines = [
        f"  {key}:",
        f"    hdx_id: {hdx_id}",
        f"    org: {org_name}",
        f"    title: {yaml.safe_dump(dataset.get('title') or '').strip()}",
        f"    themes: {kinds or ['unknown']}",
        f"    formats: {formats}",
        '    resource_filter: ""',
        f"    output_kinds: {kinds or ['tabular']}",
    ]
    return "\n".join(lines)


def audit(strict: bool = False) -> int:
    """Confirm every curated `hdx_id` still resolves live.

    Args:
        strict: When `True`, return 1 if any curated id fails to
            resolve; otherwise always return 0 (report only).

    Returns:
        int: Process exit code (0 clean, 1 on drift under `strict`).
    """
    from earthlens.hdx import Catalog
    from hdx.data.dataset import Dataset

    catalog = Catalog()
    missing: list[str] = []
    for key, row in catalog.datasets.items():
        if Dataset.read_from_hdx(row.hdx_id) is None:
            missing.append(f"{key} -> {row.hdx_id}")
    if missing:
        print(f"DRIFT: {len(missing)} curated id(s) no longer resolve on HDX:")
        for line in missing:
            print(f"  - {line}")
        return 1 if strict else 0
    print(f"OK: all {len(catalog.datasets)} curated datasets resolve on HDX.")
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Run the `refresh` subcommand.

    Searches each requested organisation (and/or tag), unions the
    results with the curated catalog's own `hdx_id`s so every curated
    key is a member of the index, and rewrites `_available.json`.
    """
    configure()
    orgs = args.org or [None]
    names: list[str] = []
    for org in orgs:
        names.extend(
            r["hdx_id"] for r in search_datasets(org=org, tag=args.tag, rows=args.rows)
        )
    if args.include_curated:
        from earthlens.hdx import Catalog

        names.extend(row.hdx_id for row in Catalog().datasets.values())
    count = write_index(names, INDEX_PATH)
    print(f"Wrote {count} dataset id(s) to {INDEX_PATH}.")
    return 0


def _cmd_add_dataset(args: argparse.Namespace) -> int:
    """Run the `add-dataset` subcommand."""
    configure()
    print(dataset_stanza(args.key, args.hdx_id))
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run the `audit` subcommand."""
    configure()
    return audit(strict=args.strict)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="rebuild the available_datasets index")
    refresh.add_argument(
        "--org", action="append", default=None, help="organisation slug (repeatable)"
    )
    refresh.add_argument("--tag", default=None, help="tag / theme filter")
    refresh.add_argument("--rows", type=int, default=1000, help="max datasets per org")
    refresh.add_argument(
        "--include-curated",
        action="store_true",
        help="union the result with the bundled catalog's curated ids",
    )
    refresh.set_defaults(func=_cmd_refresh)

    add = sub.add_parser("add-dataset", help="emit a curated stanza for one dataset")
    add.add_argument("key", help="friendly catalog key to assign")
    add.add_argument("hdx_id", help="the HDX dataset id to read")
    add.set_defaults(func=_cmd_add_dataset)

    aud = sub.add_parser("audit", help="check curated ids still resolve live")
    aud.add_argument("--strict", action="store_true", help="exit 1 on any drift")
    aud.set_defaults(func=_cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
