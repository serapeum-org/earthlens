"""Audit the radar station catalog against the live NEXRAD chunk feed.

Cross-checks the bundled `radar_data_catalog.yaml` against the top-level
station prefixes currently present in the unsigned
`unidata-nexrad-level2-chunks` bucket (the real-time feed `earthlens.radar`
fetches from). Reports, for the rolling buffer right now:

* `streaming` — catalogued sites that currently have volumes in the feed,
* `idle` — catalogued sites with no current volumes (offline / between
  scans / outside the buffer),
* `uncatalogued` — station prefixes in the feed that are not in the
  catalog (e.g. TDWR sites or ids HOMR omits).

It is the radar analogue of `tools/nwp/audit_nwp_catalog.py` /
`tools/gee/audit_gee_datasets.py`: a read-only coverage classifier, no
downloads. Because the feed is a rolling ~1–2 h buffer, "idle" is
expected for many sites at any instant — this is a liveness snapshot,
not a correctness check.

Run with:

    pixi run -e dev python tools/radar/audit_radar_catalog.py
"""

from __future__ import annotations

import argparse

from earthlens.radar import StationCatalog
from earthlens.radar.backend import BUCKET, _s3_client


def feed_stations(region: str = "us-east-1") -> set[str]:
    """Return the set of station ids currently present in the chunk feed.

    Args:
        region: AWS region of the bucket.

    Returns:
        set[str]: The top-level `{STATION}` prefixes in the feed.
    """
    client = _s3_client(region)
    out: set[str] = set()
    token: str | None = None
    while True:
        kwargs = {"Bucket": BUCKET, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        out.update(p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", []))
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            break
    return out


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default="us-east-1", help="bucket region")
    parser.add_argument(
        "--list", action="store_true", help="list the ids in each bucket"
    )
    args = parser.parse_args()

    catalog = set(StationCatalog().datasets)
    feed = feed_stations(args.region)

    streaming = sorted(catalog & feed)
    idle = sorted(catalog - feed)
    uncatalogued = sorted(feed - catalog)

    print(f"catalog sites:        {len(catalog)}")
    print(f"feed sites (now):     {len(feed)}")
    print(f"  streaming (in both): {len(streaming)}")
    print(f"  idle (catalog only): {len(idle)}")
    print(f"  uncatalogued (feed): {len(uncatalogued)}")
    if args.list:
        print("\nstreaming:", ", ".join(streaming))
        print("\nuncatalogued:", ", ".join(uncatalogued))


if __name__ == "__main__":
    main()
