"""Probe one collection's public metadata to seed a catalog row (probe).

The EUMETSAT analog of `tools/earthdata/probe_earthdata_granule.py` and
`tools/ecmwf/probe_cds_netcdf.py`. For a given collection id (or a curated
key), read its **public** browse metadata (title, abstract, product count,
sensing date range) — no credentials — and print it, optionally writing a
JSON sidecar that helps seed / vet a catalog row's `format`, `cadence`, and
`temporal` fields.

Run:

    pixi run -e dev python tools/eumetsat/probe_eumetsat_product.py \\
        EO:EUM:DAT:MSG:HRSEVIRI
    pixi run -e dev python tools/eumetsat/probe_eumetsat_product.py \\
        msg-hrseviri --out probe.json     # resolve a curated key

Uses only the public browse endpoint, so it needs no `[eumetsat]` extra
and no credentials. Not part of the installed package.
"""

from __future__ import annotations

import argparse
import json
import sys

from _store import browse_collection_detail


def _resolve_to_id(target: str) -> str:
    """Resolve a curated catalog key to its collection id, else pass it through.

    Args:
        target: Either a real `EO:EUM:DAT:…` id or a curated catalog key.

    Returns:
        str: The collection id to probe.
    """
    if target.startswith("EO:EUM:DAT:"):
        return target
    try:
        from earthlens.eumetsat import Catalog

        return Catalog().get_collection(target).collection_id
    except Exception as exc:  # noqa: BLE001 - friendly CLI message
        sys.exit(f"{target!r} is neither a collection id nor a curated key: {exc}")


def probe(target: str, out: str | None) -> int:
    """Fetch and report one collection's public browse metadata."""
    collection_id = _resolve_to_id(target)
    detail = browse_collection_detail(collection_id)
    # The browse detail document nests metadata under collection.properties
    # (a GeoJSON Feature): title / abstract / date (an ISO interval string).
    props = (detail.get("collection") or {}).get("properties") or {}
    summary = {
        "collection_id": collection_id,
        "title": props.get("title"),
        "abstract": (props.get("abstract") or "")[:200],
        "date": props.get("date"),
        "updated": props.get("updated"),
    }
    for key, value in summary.items():
        print(f"{key:18s} {value}")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(detail, fh, indent=2)
        print(f"\nwrote full metadata to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the probe."""
    parser = argparse.ArgumentParser(
        description="Probe one EUMETSAT collection's public browse metadata."
    )
    parser.add_argument(
        "target", help="a collection id (EO:EUM:DAT:...) or a curated catalog key"
    )
    parser.add_argument("--out", help="write the full metadata JSON to this path")
    args = parser.parse_args(argv)
    return probe(args.target, args.out)


if __name__ == "__main__":
    sys.exit(main())
