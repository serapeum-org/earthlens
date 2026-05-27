"""Audit the NWM configuration catalog: classify rows and (optionally) probe live.

Reads the bundled `nwm_data_catalog.yaml` and classifies every
configuration by family (`short_range` / `medium_range` / `long_range` /
`forcing` / `blend` / `coastal`), domain (`conus` / `alaska` / `hawaii`
/ `puertorico` / coastal), and whether it is an ensemble member. With
`--probe` it also HEAD-checks each config's first product/step for a
recent cycle on the unsigned `noaa-nwm-pds` bucket and reports how many
are currently available.

The NWM analogue of `tools/nwp/audit_nwp_catalog.py`: a read-only
coverage / classification view used to vet the catalog after a
`refresh_nwm_catalog.py` run.

Run with:

    pixi run -e dev python tools/nwm/audit_nwm_catalog.py
    pixi run -e dev python tools/nwm/audit_nwm_catalog.py --probe
"""

from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter

from earthlens.nwm import NWMCatalog
from earthlens.nwm.backend import BUCKET, _s3_client


def _family(key: str) -> str:
    """Classify a config key into a coarse model family."""
    if key.startswith("forcing"):
        return "forcing"
    if "blend" in key:
        return "blend"
    if "coastal" in key:
        return "coastal"
    if key.startswith("long_range"):
        return "long_range"
    if key.startswith("medium_range"):
        return "medium_range"
    if key.startswith("short_range"):
        return "short_range"
    return "other"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--probe", action="store_true", help="HEAD-check live availability"
    )
    parser.add_argument("--days-back", type=int, default=1)
    args = parser.parse_args()

    catalog = NWMCatalog()
    rows = catalog.datasets
    fam = Counter(_family(k) for k in rows)
    dom = Counter(r.domain for r in rows.values())
    ensemble = sum(1 for k in rows if "mem" in k)

    print(f"configurations: {len(rows)}")
    print(f"  by family:  {dict(sorted(fam.items()))}")
    print(f"  by domain:  {dict(sorted(dom.items()))}")
    print(f"  ensemble members: {ensemble}")

    if not args.probe:
        return

    client = _s3_client("us-east-1")
    cycle = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=args.days_back)
    available = 0
    print(f"\nprobing first product/step @ {cycle:%Y-%m-%d} (closest run hour)...")
    for key, row in rows.items():
        run = cycle.replace(hour=row.cycles_utc[0])
        s3_key = row.key_template.format(
            date=run, cycle=run, step=row.first_step, product=row.products[0]
        )
        hit = bool(client.list_objects_v2(Bucket=BUCKET, Prefix=s3_key, MaxKeys=1).get("Contents"))
        available += hit
        if not hit:
            print(f"  miss: {key} ({s3_key.rsplit('/', 1)[-1]})")
    print(f"available now: {available}/{len(rows)}")


if __name__ == "__main__":
    main()
