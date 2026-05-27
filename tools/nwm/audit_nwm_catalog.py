"""Audit `nwm_data_catalog.yaml` against the live `noaa-nwm-pds` bucket.

Diffs the curated NWM catalog against the live unsigned bucket and flags
drift:

* **Configuration availability** — every curated configuration must still
  be published under the most recent complete day (ensemble keys map to
  their `_mem1` directory). Curated-but-absent and live-but-uncurated
  configurations are reported; the catalog curates every model-output
  configuration, so the only expected "uncurated" entry is the
  `usgs_timeslices` assimilation-input directory.
* **Product tokens** — each curated product's `s3_token` (`channel_rt`,
  `land`, `reservoir`, `terrain_rt`, `forcing`, `total_water`) must appear
  among the files of at least one configuration that lists it.
* **Retention** — the live retention window is reported against the
  backend's `OPERATIONAL_RETENTION_DAYS` heuristic so the auto-mode
  boundary can be kept honest.

This is the NWM analog of `tools/{gee,chc,firms}/audit_*.py` and pairs
with `refresh_nwm_catalog.py`. It is read-only and not part of the
installed package.

Run with:

    pixi run -e dev python tools/nwm/audit_nwm_catalog.py
    pixi run -e dev python tools/nwm/audit_nwm_catalog.py --strict
    pixi run -e dev python tools/nwm/audit_nwm_catalog.py --format json

`--strict` exits non-zero when any drift is found (CI-ready).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from refresh_nwm_catalog import (  # noqa: E402
    _client,
    build_report,
    sample_products,
)

from earthlens.nwm import Catalog  # noqa: E402
from earthlens.nwm.backend import OPERATIONAL_RETENTION_DAYS  # noqa: E402


def _live_directory(catalog: Catalog, key: str) -> str:
    """Return the live bucket directory for a curated configuration key.

    Ensemble configurations live under `{key}_mem1` (member 1) rather than
    a bare `{key}` directory, so the audit probes that.
    """
    config = catalog.configurations[key]
    return f"{key}_mem1" if config.members else key


def audit(region: str) -> dict[str, Any]:
    """Compare the curated catalog against the live bucket layout."""
    report = build_report(region)
    catalog = Catalog()
    live_configs = set(report["configurations"])
    curated_configs = set(catalog.configurations)

    missing_configs = sorted(
        key
        for key in curated_configs
        if _live_directory(catalog, key) not in live_configs
    )
    # "Uncurated" excludes every member directory of a curated ensemble.
    curated_live_dirs: set[str] = set()
    for key in curated_configs:
        config = catalog.configurations[key]
        if config.members:
            curated_live_dirs.update(
                f"{key}_mem{n}" for n in range(1, config.members + 1)
            )
        else:
            curated_live_dirs.add(key)
    uncurated_configs = sorted(
        cfg for cfg in live_configs - curated_configs if cfg not in curated_live_dirs
    )

    # Each curated product token must appear on the live bucket under at
    # least one configuration that lists it. Prefer a deterministic config
    # (its file token is the bare s3_token; an ensemble appends the member).
    client = _client(region)
    day = report["sampled_day"]
    missing_tokens: list[str] = []
    for key, product in catalog.datasets.items():
        carriers = sorted(
            (
                cfgkey
                for cfgkey, cfg in catalog.configurations.items()
                if key in cfg.products
            ),
            key=lambda k: catalog.configurations[k].members,
        )
        seen = False
        for cfgkey in carriers:
            directory = _live_directory(catalog, cfgkey)
            if directory in live_configs and product.s3_token in sample_products(
                client, day, directory
            ):
                seen = True
                break
        if not seen:
            missing_tokens.append(f"{key} ({product.s3_token})")

    drift = bool(missing_configs or missing_tokens)
    return {
        "retention_days_live": report["retention"]["days_retained"],
        "retention_days_heuristic": OPERATIONAL_RETENTION_DAYS,
        "missing_configurations": missing_configs,
        "uncurated_configurations": uncurated_configs,
        "missing_product_tokens": missing_tokens,
        "drift": drift,
    }


def _as_text(result: dict[str, Any]) -> str:
    """Render the audit result as a human-readable summary."""
    lines = [
        f"retention: live {result['retention_days_live']} days "
        f"(heuristic boundary {result['retention_days_heuristic']} days)",
        f"missing configurations (curated, not live): "
        f"{result['missing_configurations'] or 'none'}",
        f"uncurated configurations (live, not curated): "
        f"{len(result['uncurated_configurations'])} "
        f"{result['uncurated_configurations']}",
        f"missing product tokens: {result['missing_product_tokens'] or 'none'}",
        f"drift: {'YES' if result['drift'] else 'no'}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the audit and print the result; honour `--strict`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on any drift"
    )
    args = parser.parse_args(argv)

    result = audit(args.region)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(_as_text(result))
    return 1 if (args.strict and result["drift"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
