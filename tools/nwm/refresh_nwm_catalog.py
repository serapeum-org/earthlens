"""Regenerate `earthlens/nwm/nwm_data_catalog.yaml` from the live NWM bucket.

Enumerates the configuration directories under a recent `nwm.{YYYYMMDD}/`
date on the unsigned `noaa-nwm-pds` bucket and, for every **forecast**
configuration (one that publishes `fNNN` lead-time files — as opposed to
the `tmNN` analyses, which do not fit the `(cycle, step)` axis the
backend models), infers a `NWMConfig` row:

* `cycles_utc` — the run hours present (cheap `MaxKeys=1` probe per hour),
* `products` — the distinct product tokens in a sample cycle,
* `horizon_h` / `step_cadence_h` — from the observed `fNNN` range,
* `domain` and `key_template` — reconstructed from a real key by
  substituting the date / cycle / step / product fields. Reconstructing
  from an actual file name means the irregular cases fall out for free:
  the ensemble member riding on the product token (`channel_rt_1`),
  regional sub-hourly 5-digit steps (`f00015`), and the dir-vs-filename
  token mismatch all reproduce exactly.

It is the NWM analogue of `tools/nwp/refresh_nwp_catalog.py`. Analyses
(`analysis_assim*`) and `usgs_timeslices` are skipped (no `fNNN`).

Run with:

    pixi run -e dev python tools/nwm/refresh_nwm_catalog.py --dry-run
    pixi run -e dev python tools/nwm/refresh_nwm_catalog.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from math import gcd
from pathlib import Path
from typing import Any

from earthlens.nwm.backend import BUCKET, _s3_client
from earthlens.nwm.catalog import CATALOG_PATH

_FXX = re.compile(r"\.f(\d+)\.")


def _list(client: Any, prefix: str, delimiter: str | None = None, cap: int | None = None) -> dict:
    """Paginated `list_objects_v2`, accumulating keys (and common prefixes)."""
    keys: list[str] = []
    prefixes: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": BUCKET, "Prefix": prefix}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated") or (cap and len(keys) >= cap):
            break
    return {"keys": keys, "prefixes": prefixes}


def _has(client: Any, prefix: str) -> bool:
    """True if at least one object exists under `prefix`."""
    return bool(client.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=1).get("Contents"))


def infer_config(client: Any, date: dt.datetime, config_dir: str) -> dict | None:
    """Infer one forecast config's row from the bucket, or `None` if not forecast.

    Args:
        client: The unsigned S3 client.
        date: The probe date.
        config_dir: The configuration directory name (`"short_range"`).

    Returns:
        dict | None: A `NWMConfig`-shaped dict, or `None` for a
            non-forecast / empty configuration.
    """
    base = f"nwm.{date:%Y%m%d}/{config_dir}/"
    cycles = [hh for hh in range(24) if _has(client, f"{base}nwm.t{hh:02d}z.")]
    if not cycles:
        return None
    sample = _list(client, f"{base}nwm.t{cycles[0]:02d}z.")["keys"]
    fxx_files = [k for k in sample if _FXX.search(k)]
    if not fxx_files:
        return None  # analysis (tmNN) or other non-forecast layout

    steps, products, widths, domains = set(), set(), set(), set()
    for key in fxx_files:
        name = key.rsplit("/", 1)[-1]
        m = _FXX.search(name)
        digits = m.group(1)
        steps.add(int(digits))
        widths.add(len(digits))
        # name = nwm.t{HH}z.{token}.{product}.f{NNN}.{domain}.nc
        before = name[: m.start()]          # nwm.tHHz.{token}.{product}
        after = name[m.end():]              # {domain}.nc
        body = before.split("z.", 1)[1]     # {token}.{product}
        token, product = body.split(".", 1)
        products.add(product)
        domains.add(after.rsplit(".", 1)[0])

    sorted_steps = sorted(steps)
    diffs = [b - a for a, b in zip(sorted_steps, sorted_steps[1:]) if b > a]
    cadence = 0
    for d in diffs:
        cadence = gcd(cadence, d)
    width = max(widths)
    domain = sorted(domains)[0]
    # Rebuild the key template from a representative file name.
    rep = sorted(fxx_files)[0].rsplit("/", 1)[-1]
    rep_body = rep.split("z.", 1)[1]
    token = rep_body.split(".", 1)[0]
    tmpl_name = re.sub(r"\.f\d+\.", f".f{{step:0{width}d}}.", rep)
    tmpl_name = re.sub(r"nwm\.t\d\dz\.", "nwm.t{cycle:%H}z.", tmpl_name)
    one_product = sorted(products)[0]
    tmpl_name = tmpl_name.replace(f".{token}.{one_product}.", f".{token}.{{product}}.", 1)
    key_template = f"nwm.{{date:%Y%m%d}}/{config_dir}/{tmpl_name}"
    return {
        "domain": domain,
        "cycles_utc": cycles,
        "first_step": sorted_steps[0],
        "horizon_h": sorted_steps[-1],
        "step_cadence_h": cadence or 1,
        "products": sorted(products),
        "key_template": key_template,
    }


def render_yaml(rows: dict[str, dict]) -> str:
    """Render the `configurations:` YAML block."""
    lines = [
        "# National Water Model (NOAA NWM) configuration catalog.",
        "#",
        "# Regenerated by tools/nwm/refresh_nwm_catalog.py from the noaa-nwm-pds",
        "# bucket. Only forecast configurations (fNNN lead times) are emitted; the",
        "# tmNN analyses do not fit the (cycle, step) axis and are skipped.",
        "",
        "configurations:",
    ]
    for key, row in rows.items():
        lines.append("")
        lines.append(f"  {key}:")
        lines.append(f"    domain: {row['domain']}")
        lines.append(f"    cycles_utc: {row['cycles_utc']}")
        lines.append(f"    first_step: {row['first_step']}")
        lines.append(f"    horizon_h: {row['horizon_h']}")
        lines.append(f"    step_cadence_h: {row['step_cadence_h']}")
        lines.append(f"    products: [{', '.join(row['products'])}]")
        lines.append(f'    key_template: >-\n      {row["key_template"]}')
    return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days-back", type=int, default=1, help="probe date offset")
    parser.add_argument("--dry-run", action="store_true", help="summarise, do not write")
    parser.add_argument("--output", type=Path, default=CATALOG_PATH)
    args = parser.parse_args()

    client = _s3_client("us-east-1")
    date = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=args.days_back)
    config_dirs = [p.rstrip("/").rsplit("/", 1)[-1]
                   for p in _list(client, f"nwm.{date:%Y%m%d}/", delimiter="/")["prefixes"]]
    print(f"{date:%Y-%m-%d}: {len(config_dirs)} config dirs; inferring forecast rows...")

    rows: dict[str, dict] = {}
    for cd in config_dirs:
        row = infer_config(client, date, cd)
        if row:
            rows[cd] = row
            print(f"  + {cd}: cycles={len(row['cycles_utc'])} horizon={row['horizon_h']} "
                  f"products={len(row['products'])}")
    print(f"forecast configs: {len(rows)} (of {len(config_dirs)} dirs)")
    if args.dry_run:
        return
    args.output.write_text(render_yaml(rows), encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
