"""Maintain the bundled HDX data catalog (`src/earthlens/hdx/catalog/`).

A single `argparse` subcommand CLI over the read-only `hdx-python-api`
client. Run with no args to see the subcommand list:

    pixi run -e dev python tools/hdx/refresh_hdx_catalog.py --help

Subcommands:

* `refresh` — rebuild the `available_datasets` index in
  `catalog/_available.json`. With `--all`, enumerate the **entire** HDX
  catalogue (~41k ids) via `Dataset.get_all_dataset_names()`; otherwise
  filter by `--org` / `--tag` via `Dataset.search_in_hdx(...)`. Every id
  in this index resolves to a thin `HdxDataset` through
  `Catalog.get_dataset` (the long-tail fallback), so any HDX dataset is
  usable by key — the analogue of `tools/gee/refresh_gee_catalog.py`'s
  `available_datasets:` block plus `earthlens.earthdata`'s `_auto.json`.
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
from typing import Any

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


def search_metadata(
    query: str = "*:*",
    fq: str | None = None,
    page_size: int = 1000,
) -> dict[str, dict]:
    """Page CKAN `package_search` for lightweight `{id: {org, title}}` rows.

    Uses the SDK's `remoteckan` client with a `fl` field-list so only
    `name` / `organization` / `title` come back (no heavy resource
    payloads), paginating until every match is collected.

    Args:
        query: CKAN free-text query (default `"*:*"`, every searchable
            dataset).
        fq: Optional CKAN filter query (e.g. `"organization:kontur"`).
        page_size: Rows per request.

    Returns:
        dict[str, dict]: Map from HDX id to its `{org, title}` row.
    """
    from hdx.api.configuration import Configuration

    client = Configuration.read().remoteckan()
    out: dict[str, dict] = {}
    start = 0
    while True:
        params: dict[str, Any] = {
            "q": query,
            "fl": "name,organization,title",
            "rows": page_size,
            "start": start,
        }
        if fq:
            params["fq"] = fq
        result = client.call_action("package_search", params)
        batch = result.get("results") or []
        for row in batch:
            org = row.get("organization")
            # With a `fl` field-list, CKAN returns `organization` as the org
            # slug string; without it, as a dict. Handle both.
            org_name = org.get("name") if isinstance(org, dict) else (org or "")
            out[row["name"]] = {
                "org": org_name or "",
                "title": row.get("title") or "",
            }
        start += len(batch)
        if not batch or start >= result.get("count", 0):
            break
    return out


def all_dataset_names() -> list[str]:
    """Return every HDX dataset id (the whole `data.humdata.org` catalogue).

    Wraps `Dataset.get_all_dataset_names()` — one cheap paginated call
    that returns all ~41k ids (including the non-searchable long tail
    that `package_search` omits) without per-dataset requests.

    Returns:
        list[str]: Every HDX dataset id / name.
    """
    from hdx.data.dataset import Dataset

    return list(Dataset.get_all_dataset_names())


def all_metadata() -> dict[str, dict]:
    """Build the enriched index for the **entire** HDX catalogue.

    Unions every id from :func:`all_dataset_names` (the complete ~41k
    universe) with the `{org, title}` enrichment from
    :func:`search_metadata` (the ~28k `package_search` exposes). Ids the
    search does not expose get an empty `{org, title}` row but stay
    resolvable.

    Returns:
        dict[str, dict]: Map from every HDX id to its `{org, title}` row.
    """
    enriched = search_metadata("*:*")
    return {
        name: enriched.get(name, {"org": "", "title": ""})
        for name in all_dataset_names()
    }


def write_index(rows: dict[str, dict], index_path: Path = INDEX_PATH) -> int:
    """Rewrite the enriched long-tail JSON index `{hdx_id: {org, title}}`.

    The index is JSON (`_available.json`), kept out of the curated
    `*.yaml` glob so `Catalog()` parses only the small curated YAMLs and
    reads this map separately (the `earthlens.earthdata` `_auto.json`
    pattern). Every id here resolves to a synthesised `HdxDataset` via
    `Catalog.get_dataset`.

    Args:
        rows: Map from HDX id to its `{org, title}` row.
        index_path: Path to `catalog/_available.json`.

    Returns:
        int: The number of ids written.
    """
    import json

    datasets = {
        name: {
            "org": (body or {}).get("org", ""),
            "title": (body or {}).get("title", ""),
        }
        for name, body in sorted(rows.items())
    }
    payload = {
        "__comment__": (
            "AUTO-GENERATED by tools/hdx/refresh_hdx_catalog.py (refresh --all). "
            "Every HDX dataset id with its org/title; any id here resolves to a "
            "synthesised HdxDataset via Catalog.get_dataset (the earthdata _auto "
            "fallback). NOT vetted; the curated per-theme YAMLs carry full "
            "metadata. Out of the *.yaml glob so Catalog() stays fast."
        ),
        "datasets": datasets,
    }
    index_path.write_text(
        json.dumps(payload, indent=0, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return len(datasets)


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

    With `--all`, enumerates the **entire** HDX catalogue via
    `Dataset.get_all_dataset_names()` (one cheap paginated call, ~41k
    ids). Otherwise searches each requested organisation (and/or tag).
    Either way the result is unioned with the curated catalog's own
    `hdx_id`s so every curated key is a member, and `_available.json` is
    rewritten.
    """
    configure()
    if args.all:
        rows = all_metadata()
    else:
        rows = {}
        for org in args.org or [None]:
            filters = []
            if org:
                filters.append(f"organization:{org}")
            if args.tag:
                filters.append(f"tags:{args.tag}")
            rows.update(search_metadata(fq=" AND ".join(filters) or None))
    if args.include_curated:
        from earthlens.hdx import Catalog

        for row in Catalog().datasets.values():
            rows.setdefault(row.hdx_id, {"org": row.org, "title": row.title})
    count = write_index(rows, INDEX_PATH)
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
    refresh.add_argument(
        "--all",
        action="store_true",
        help="enumerate the entire HDX catalogue via get_all_dataset_names()",
    )
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
