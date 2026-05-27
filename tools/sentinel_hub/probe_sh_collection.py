"""Probe a Sentinel Hub collection's metadata for curating a `collections.yaml` row.

Prints whether a name is a real `sentinelhub.DataCollection` member and, when the
collection is already curated, its bound id, native resolution, cadence, extent,
and band list — the fields you copy into `catalog/collections.yaml`. The Sentinel
Hub analogue of `tools/openeo/probe_openeo_collection.py` /
`tools/stac/probe_stac_assets.py`.

    python tools/sentinel_hub/probe_sh_collection.py sentinel-2-l2a
    python tools/sentinel_hub/probe_sh_collection.py SENTINEL2_L1C --yaml

Resolving the enum is **offline** (no credentials). `--yaml` emits a ready-to
-paste `collections.yaml` stanza for an already-curated key. Exits 0 on success,
1 when the name is neither a curated key nor a `DataCollection` member. Not part
of the installed package.
"""

from __future__ import annotations

import argparse
import sys

import yaml


def _enum_members() -> set[str]:
    """Return the set of `sentinelhub.DataCollection` member names (offline)."""
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    sentinelhub = import_sentinelhub()
    return {member.name for member in sentinelhub.DataCollection}


def probe(name: str, as_yaml: bool = False) -> int:
    """Print metadata for a curated key or a raw `DataCollection` member.

    Args:
        name: A curated collection key (e.g. `sentinel-2-l2a`) or a raw
            `DataCollection` member name (e.g. `SENTINEL2_L2A`).
        as_yaml: When `True`, emit a `collections.yaml` stanza for a curated key.

    Returns:
        Process exit code (0 success, 1 when `name` is unknown).
    """
    from earthlens.sentinel_hub import Catalog

    catalog = Catalog()
    members = _enum_members()

    if name in catalog.datasets:
        collection = catalog.datasets[name]
        in_enum = collection.sh_collection in members
        if as_yaml:
            stanza = {
                name: {
                    "sh_collection": collection.sh_collection,
                    "description": collection.description,
                    "cadence": collection.cadence,
                    "resolution": collection.resolution,
                    "default_bands": collection.default_bands,
                    "bands": {b: {} for b in collection.bands},
                }
            }
            sys.stdout.write(yaml.safe_dump({"collections": stanza}, sort_keys=False))
            return 0
        sys.stdout.write(
            f"{name}: curated -> DataCollection.{collection.sh_collection} "
            f"({'in enum' if in_enum else 'NOT in enum!'})\n"
            f"  resolution: {collection.resolution} m   cadence: {collection.cadence}\n"
            f"  default_bands: {collection.default_bands}\n"
            f"  bands: {sorted(collection.bands)}\n"
        )
        return 0

    if name in members:
        sys.stdout.write(
            f"{name}: a DataCollection member, not yet curated. Add a "
            "collections.yaml row binding a logical key to it.\n"
        )
        return 0

    sys.stderr.write(
        f"{name!r} is neither a curated collection key nor a DataCollection member. "
        f"Known curated keys: {sorted(catalog.datasets)}.\n"
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="a curated key or a DataCollection member name")
    parser.add_argument(
        "--yaml", action="store_true", help="emit a collections.yaml stanza"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to `sys.argv`).

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return probe(args.name, as_yaml=args.yaml)


if __name__ == "__main__":
    raise SystemExit(main())
