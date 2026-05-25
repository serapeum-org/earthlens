"""Probe a STAC collection's asset/band schema from a sample item.

For one `(endpoint, collection)` pair this fetches a single sample item and
records each asset's media type plus the per-band metadata STAC items carry in
the `eo:bands` / `raster:bands` extensions (common name, data type, nodata) —
the seed for the curated `assets:` block in a per-endpoint catalog file. It is
the STAC analogue of ``tools/ecmwf/probe_cds_netcdf.py``.

    python tools/stac/probe_stac_assets.py probe earth-search sentinel-2-l2a

The schema is printed as JSON (or written with ``--out path.json``). It is a
*seed*, not a drop-in: review the dtypes/nodata and trim to the assets worth
curating before pasting into the catalog. Exits 0 on success, 1 on any HTTP /
parse error or when the collection yields no items. Not part of the installed
package.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _asset_fields(asset: Any) -> dict[str, Any]:
    """Return a plain field dict for a pystac `Asset` or a raw STAC asset dict.

    Args:
        asset: A pystac `Asset` (with `media_type` / `extra_fields`) or a raw
            STAC asset mapping.

    Returns:
        A dict exposing `type`, `eo:bands`, `raster:bands` (missing keys absent).

    Examples:
        - A raw STAC asset dict is returned as-is:
            ```python
            >>> _asset_fields({"href": "x.tif", "type": "image/tiff"})["type"]
            'image/tiff'

            ```
        - A pystac-like asset is normalised to a field dict:
            ```python
            >>> from types import SimpleNamespace
            >>> asset = SimpleNamespace(media_type="image/tiff", extra_fields={"raster:bands": [{"data_type": "int16"}]})
            >>> _asset_fields(asset)["raster:bands"][0]["data_type"]
            'int16'

            ```
    """
    if isinstance(asset, dict):
        return asset
    fields: dict[str, Any] = dict(getattr(asset, "extra_fields", {}) or {})
    media_type = getattr(asset, "media_type", None)
    if media_type is not None:
        fields.setdefault("type", media_type)
    return fields


def _asset_schema(item: Any) -> dict[str, dict[str, Any]]:
    """Extract a per-asset schema from a STAC item.

    For each asset, reads the media type and the first `raster:bands` /
    `eo:bands` entry to recover `dtype`, `nodata`, and `common_name` — the
    fields the catalog `Asset` model curates.

    Args:
        item: A pystac `Item` or a raw STAC item dict with an `assets` mapping.

    Returns:
        Mapping of asset key → `{media_type, common_name, dtype, nodata}` (with
        `None` for fields the item does not carry).

    Examples:
        - Recover the band schema from a STAC item's assets:
            ```python
            >>> item = {"assets": {"B04": {"type": "image/tiff",
            ...     "eo:bands": [{"common_name": "red"}],
            ...     "raster:bands": [{"data_type": "uint16", "nodata": 0}]}}}
            >>> _asset_schema(item)["B04"]["common_name"]
            'red'
            >>> _asset_schema(item)["B04"]["dtype"]
            'uint16'

            ```
        - An asset without band extensions yields None fields:
            ```python
            >>> _asset_schema({"assets": {"data": {"type": "image/tiff"}}})["data"]["dtype"] is None
            True

            ```
    """
    assets = getattr(item, "assets", None)
    if assets is None and isinstance(item, dict):
        assets = item.get("assets", {})
    schema: dict[str, dict[str, Any]] = {}
    for key, asset in (assets or {}).items():
        fields = _asset_fields(asset)
        raster_bands = fields.get("raster:bands") or [{}]
        eo_bands = fields.get("eo:bands") or [{}]
        first_raster = raster_bands[0] if raster_bands else {}
        first_eo = eo_bands[0] if eo_bands else {}
        schema[key] = {
            "media_type": fields.get("type"),
            "common_name": first_eo.get("common_name"),
            "dtype": first_raster.get("data_type"),
            "nodata": first_raster.get("nodata"),
        }
    return schema


def _cmd_probe(args: argparse.Namespace) -> int:
    """Fetch one sample item for a collection and dump its asset schema.

    Args:
        args: Parsed CLI args (`endpoint`, `collection`, `out`).

    Returns:
        Process exit code (0 success, 1 on no items / error).
    """
    from earthlens.stac.catalog import Catalog
    from pyramids.stac import open_client

    catalog = Catalog()
    try:
        endpoint = catalog.get_endpoint(args.endpoint)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    resolved = catalog.resolve(args.endpoint, args.collection)

    client = open_client(endpoint.url)
    search = client.search(collections=[resolved], max_items=1)
    items = list(search.items())
    if not items:
        print(
            f"no items found for {args.collection!r} ({resolved}) at {args.endpoint}",
            file=sys.stderr,
        )
        return 1

    schema = _asset_schema(items[0])
    payload = json.dumps(
        {"endpoint": args.endpoint, "collection": args.collection,
         "resolved": resolved, "assets": schema},
        indent=2,
    )
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to `sys.argv[1:]`).

    Returns:
        Process exit code.
    """
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_probe = sub.add_parser("probe", help="dump a collection's asset/band schema")
    p_probe.add_argument("endpoint", help="endpoint key (e.g. earth-search)")
    p_probe.add_argument("collection", help="logical collection key (e.g. sentinel-2-l2a)")
    p_probe.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    p_probe.set_defaults(func=_cmd_probe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
