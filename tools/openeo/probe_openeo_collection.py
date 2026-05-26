"""Probe a single openEO collection's live metadata (bands, extent, dimensions).

A small helper for curating a new collection row: it calls
`Connection.describe_collection(<id>)` and prints the band list, spatial /
temporal extent, ground sample distance, and cube dimensions — the fields you
copy into `catalog/collections.yaml`. The openEO analogue of
`tools/stac/probe_stac_assets.py` / `tools/cmems/probe_cmems_netcdf.py`.

    python tools/openeo/probe_openeo_collection.py SENTINEL2_L2A
    python tools/openeo/probe_openeo_collection.py SENTINEL_5P_L2 --yaml

`describe_collection` is **anonymous** (no OIDC login). `--yaml` emits a ready-to
-paste `collections.yaml` stanza (you still pick `default_bands` + a description).
Exits 0 on success, 1 on any error. Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_ENDPOINT = "https://openeo.dataspace.copernicus.eu"


def _describe(endpoint: str, collection_id: str) -> dict:
    """Return the backend's collection metadata document.

    Args:
        endpoint: openEO API root URL.
        collection_id: The UPPERCASE openEO collection id.

    Returns:
        The collection metadata dict.

    Raises:
        RuntimeError: When the collection cannot be described.
    """
    import openeo

    try:
        return openeo.connect(endpoint).describe_collection(collection_id)
    except Exception as exc:  # noqa: BLE001 - re-raise with context
        raise RuntimeError(
            f"failed to describe {collection_id!r} at {endpoint}: {exc}"
        ) from exc


def _bands(metadata: dict) -> list[str]:
    """Extract the band list from collection metadata (eo:bands or cube dims).

    Args:
        metadata: The collection metadata dict.

    Returns:
        The band names, or an empty list when none are declared.
    """
    eo_bands = metadata.get("summaries", {}).get("eo:bands", [])
    names = [b.get("name") for b in eo_bands if b.get("name")]
    if names:
        return names
    for dim in metadata.get("cube:dimensions", {}).values():
        if dim.get("type") == "bands":
            return list(dim.get("values") or [])
    return []


def _slug(collection_id: str) -> str:
    """Return a lowercase-hyphenated logical key suggestion for a collection id."""
    return collection_id.lower().replace("_", "-")


def _cmd_probe(args: argparse.Namespace) -> int:
    """Describe one collection and print its curation-relevant fields.

    Args:
        args: Parsed CLI args (`collection_id`, `endpoint`, `yaml`).

    Returns:
        Process exit code (0 success, 1 on error).
    """
    try:
        metadata = _describe(args.endpoint, args.collection_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    bands = _bands(metadata)
    extent = metadata.get("extent", {})
    spatial = extent.get("spatial", {}).get("bbox")
    temporal = extent.get("temporal", {}).get("interval")
    gsd = metadata.get("summaries", {}).get("gsd")

    if args.yaml:
        start = (temporal or [[None]])[0][0]
        start = start[:10] if isinstance(start, str) else None
        print(f"  {_slug(args.collection_id)}:")
        print(f"    collection_id: {args.collection_id}")
        print(f"    description: {metadata.get('title', '')!r}")
        if gsd:
            print(f"    resolution: {float(gsd[0])}")
        bands_str = ", ".join(bands)
        print(f"    bands: [{bands_str}]")
        print(f"    default_bands: [{bands[0] if bands else ''}]")
        if start:
            print("    extent:")
            print(f"      start_date: {start!r}")
        return 0

    print(f"collection: {args.collection_id}")
    print(f"  title:     {metadata.get('title', '')}")
    print(f"  bands ({len(bands)}): {bands}")
    print(f"  spatial bbox: {spatial}")
    print(f"  temporal:     {temporal}")
    print(f"  gsd:          {gsd}")
    return 0


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
    parser.add_argument("collection_id", help="UPPERCASE openEO collection id to probe")
    parser.add_argument(
        "--endpoint", default=DEFAULT_ENDPOINT, help="openEO API root URL"
    )
    parser.add_argument(
        "--yaml",
        action="store_true",
        help="emit a ready-to-paste collections.yaml stanza",
    )
    parser.set_defaults(func=_cmd_probe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
