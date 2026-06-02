"""Maintain / validate the bundled AWS Open-Data S3 catalog (`src/earthlens/s3/`).

A single `argparse` subcommand CLI over unsigned `boto3` listings. Run
with no args to see the subcommand list:

    pixi run -e dev python tools/s3/refresh_s3_catalog.py --help

Subcommands:

* `refresh` — rewrite the informational `available_datasets:` block of
  `s3_data_catalog.yaml` from the curated dataset names, preserving the
  `datasets:` block and comments. Reloads the catalog at the end so a
  broken rewrite fails the run.
* `validate` — for every registered dataset, confirm a representative S3
  object is reachable on its bucket (a deterministic tile or a listed
  prefix). `--strict` exits 1 on any drift (a dataset whose bucket no
  longer serves the expected layout).
* `probe <dataset>` — list a small slice of a dataset's bucket and print
  the first object keys (handy when curating a new dataset).

Exits 0 on success, 1 on any listing / drift error. Not part of the
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

from earthlens.s3.catalog import CATALOG_PATH, Catalog  # noqa: E402

#: Matches the trailing `available_datasets:` block (to EOF) so `refresh`
#: replaces only that block and keeps the curated `datasets:` + comments.
_AVAILABLE_BLOCK = re.compile(r"^available_datasets:.*\Z", re.DOTALL | re.MULTILINE)

#: A representative prefix per dataset for `validate` / `probe`.
_PROBE_PREFIX: dict[str, str] = {
    "era5": "e5.oper.an.sfc/202312/",
    "sentinel-2-l2a": "sentinel-s2-l2a-cogs/36/R/UU/",
    "goes": "ABI-L2-CMIPF/2024/180/",
    "copernicus-dem": "Copernicus_DSM_COG_10_N00_00_E006_00_DEM/",
    "esa-worldcover": "v200/2021/map/",
}


def _client():
    """Build an unsigned S3 client (lazy import keeps the tool importable)."""
    import boto3
    import botocore.client

    return boto3.client(
        "s3", config=botocore.client.Config(signature_version=botocore.UNSIGNED)
    )


def _list(client, bucket: str, prefix: str, limit: int = 5) -> list[str]:
    """Return up to `limit` object keys under `prefix` (empty if none)."""
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=limit)
    return [obj["Key"] for obj in response.get("Contents", [])]


def refresh(_args: argparse.Namespace) -> int:
    """Rewrite the `available_datasets:` index from the curated dataset names."""
    catalog = Catalog()
    names = catalog.dataset_names()
    block = "available_datasets:\n" + "".join(f"  - {n}\n" for n in names)
    text = CATALOG_PATH.read_text(encoding="utf-8")
    if _AVAILABLE_BLOCK.search(text):
        text = _AVAILABLE_BLOCK.sub(block.rstrip() + "\n", text)
    else:
        text = text.rstrip() + "\n\n" + block
    CATALOG_PATH.write_text(text, encoding="utf-8", newline="\n")
    Catalog.load()  # fail loudly if the rewrite broke the YAML
    print(f"refreshed available_datasets ({len(names)}): {names}")
    return 0


def validate(args: argparse.Namespace) -> int:
    """Confirm each registered dataset's bucket still serves its layout."""
    catalog = Catalog()
    client = _client()
    drift: list[str] = []
    for name in catalog.dataset_names():
        dataset = catalog.resolve(name)
        prefix = _PROBE_PREFIX.get(name, "")
        keys = _list(client, dataset.bucket, prefix)
        status = "ok" if keys else "NO OBJECTS"
        if not keys:
            drift.append(name)
        print(f"{name:16s} s3://{dataset.bucket}/{prefix}  -> {status}")
    if drift:
        print(f"\nDRIFT: {drift}")
        return 1 if args.strict else 0
    print("\nall datasets reachable.")
    return 0


def probe(args: argparse.Namespace) -> int:
    """List a small slice of one dataset's bucket and print the first keys."""
    catalog = Catalog()
    dataset = catalog.resolve(args.dataset)
    prefix = _PROBE_PREFIX.get(args.dataset, "")
    keys = _list(_client(), dataset.bucket, prefix, limit=args.limit)
    print(f"s3://{dataset.bucket}/{prefix}  ({len(keys)} keys)")
    for key in keys:
        print(f"  {key}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the subcommand CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("refresh", help="rewrite available_datasets:").set_defaults(func=refresh)

    validate_parser = sub.add_parser("validate", help="check each dataset's bucket")
    validate_parser.add_argument("--strict", action="store_true", help="exit 1 on drift")
    validate_parser.set_defaults(func=validate)

    probe_parser = sub.add_parser("probe", help="list a dataset's bucket slice")
    probe_parser.add_argument("dataset", help="a registered dataset name")
    probe_parser.add_argument("--limit", type=int, default=10)
    probe_parser.set_defaults(func=probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
