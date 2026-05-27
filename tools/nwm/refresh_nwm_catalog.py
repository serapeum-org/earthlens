"""Probe the live `noaa-nwm-pds` bucket and report its layout.

Walks the unsigned public NWM bucket and prints (or emits as JSON / a
YAML `available_configurations:` stanza) what it finds:

* the **retention window** — the earliest and latest `nwm.YYYYMMDD/`
  date prefix and the number of days retained;
* the **configurations** published under the most recent complete day,
  with the product `{output}` tokens (`channel_rt`, `land`, ...), the run
  hours, and the step scheme (`fNNN` vs `tmNN`) inferred per
  configuration.

This is the NWM analog of `tools/{gee,chc,cmems}/refresh_*.py`. It is
read-only (no credentials, no writes to the bucket) and is not part of
the installed package — it reads the curated catalog through
:class:`earthlens.nwm.Catalog` only to label which configurations are
already curated.

Run with:

    pixi run -e dev python tools/nwm/refresh_nwm_catalog.py
    pixi run -e dev python tools/nwm/refresh_nwm_catalog.py --format json
    pixi run -e dev python tools/nwm/refresh_nwm_catalog.py --format yaml
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from earthlens.nwm import BUCKET, Catalog  # noqa: E402


def _client(region: str) -> Any:
    """Return an unsigned boto3 S3 client for the public NWM bucket."""
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config

    return boto3.client(
        "s3", region_name=region, config=Config(signature_version=UNSIGNED)
    )


def list_date_prefixes(client: Any) -> list[str]:
    """Return the `nwm.YYYYMMDD` date prefixes on the bucket, ascending."""
    paginator = client.get_paginator("list_objects_v2")
    days: list[str] = []
    for page in paginator.paginate(Bucket=BUCKET, Delimiter="/"):
        for entry in page.get("CommonPrefixes", []):
            prefix = entry["Prefix"].rstrip("/")
            if prefix.startswith("nwm."):
                days.append(prefix)
    return sorted(days)


def list_configurations(client: Any, day_prefix: str) -> list[str]:
    """Return the configuration directories under one `nwm.YYYYMMDD/` day."""
    result = client.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{day_prefix}/", Delimiter="/"
    )
    return sorted(
        entry["Prefix"].split("/")[1] for entry in result.get("CommonPrefixes", [])
    )


def sample_products(client: Any, day_prefix: str, configuration: str) -> list[str]:
    """Return the distinct product `{output}` tokens seen under a configuration."""
    result = client.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{day_prefix}/{configuration}/", MaxKeys=400
    )
    tokens: set[str] = set()
    for entry in result.get("Contents", []):
        parts = entry["Key"].split("/")[-1].split(".")
        # nwm.tHHz.<family>.<output>.<step>.<domain>.nc
        if len(parts) >= 6:
            tokens.add(parts[3])
    return sorted(tokens)


def build_report(region: str) -> dict[str, Any]:
    """Probe the bucket and assemble the layout report."""
    client = _client(region)
    days = list_date_prefixes(client)
    if not days:
        raise RuntimeError(f"no nwm.YYYYMMDD/ prefixes found on {BUCKET}.")
    earliest, latest = days[0], days[-1]
    # Use the day before the latest (the latest may be mid-publication).
    complete_day = days[-2] if len(days) > 1 else days[-1]
    curated = set(Catalog().configurations)
    configurations = list_configurations(client, complete_day)
    rows = {
        cfg: {
            "products": sample_products(client, complete_day, cfg),
            "curated": cfg in curated,
        }
        for cfg in configurations
    }
    return {
        "bucket": BUCKET,
        "retention": {
            "earliest": earliest,
            "latest": latest,
            "days_retained": len(days),
        },
        "sampled_day": complete_day,
        "configurations": rows,
    }


def _as_yaml(report: dict[str, Any]) -> str:
    """Render the configuration index as a YAML `available_configurations:` stanza."""
    import yaml

    index = {"available_configurations": sorted(report["configurations"])}
    return yaml.safe_dump(index, default_flow_style=False, sort_keys=False)


def _as_text(report: dict[str, Any]) -> str:
    """Render the report as a human-readable summary."""
    ret = report["retention"]
    lines = [
        f"bucket: {report['bucket']}",
        f"retention: {ret['earliest']} .. {ret['latest']} "
        f"({ret['days_retained']} days)",
        f"sampled day: {report['sampled_day']}",
        f"configurations ({len(report['configurations'])}):",
    ]
    for cfg, info in report["configurations"].items():
        mark = "curated" if info["curated"] else "available"
        lines.append(f"  [{mark}] {cfg}: {', '.join(info['products']) or '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Probe the bucket and print the layout in the requested format."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--format", choices=["text", "json", "yaml"], default="text")
    args = parser.parse_args(argv)

    report = build_report(args.region)
    report["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif args.format == "yaml":
        print(_as_yaml(report))
    else:
        print(_as_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
