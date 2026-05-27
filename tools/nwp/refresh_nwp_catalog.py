"""Inspect / probe the bundled NWP catalog (`src/earthlens/nwp/nwp_data_catalog.yaml`).

An ``argparse`` CLI that summarises the curated NWP models and, with
``--live``, probes each model's source for the latest available cycle.
Probing is best-effort and centre-specific: Herbie / ECMWF models are
reported from their catalog metadata (a full availability walk would
need the SDKs), and ``direct-https`` models (DWD ICON) are checked with
a cheap HTTP ``HEAD`` against the most recent expected cycle URL.

Run with no args (or ``--help``) to see the summary offline:

    pixi run -e dev python tools/nwp/refresh_nwp_catalog.py
    pixi run -e dev python tools/nwp/refresh_nwp_catalog.py --live
"""

from __future__ import annotations

import argparse
import datetime as dt

from earthlens.nwp import Catalog
from earthlens.nwp.catalog import NWPModel


def _latest_cycle(model: NWPModel, hours_ago: int = 6) -> dt.datetime | None:
    """Return the most recent run datetime for a model, or None if it has no cycles."""
    if not model.cycles_utc:
        return None
    moment = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=hours_ago)
    hours = sorted(model.cycles_utc)
    for day_offset in (0, 1):
        day = moment - dt.timedelta(days=day_offset)
        for hour in reversed(hours):
            candidate = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= moment:
                return candidate
    return None


def _probe_direct_https(model: NWPModel, cycle: dt.datetime) -> str:
    """HEAD the first band's URL for `cycle`; return a short status string."""
    if not model.url_template or not model.bands:
        return "no url_template"
    import requests

    var = next(iter(model.bands.values()))
    url = model.url_template.format(
        cycle=cycle, date=cycle, step=0, var=var, var_lc=var.lower()
    )
    try:
        resp = requests.head(url, timeout=30, allow_redirects=True)
        return f"HTTP {resp.status_code}"
    except Exception as exc:  # network flake — report, do not raise
        return f"unreachable ({type(exc).__name__})"


def summarise(live: bool) -> int:
    """Print one row per curated model; probe direct-HTTPS models when `live`.

    Args:
        live: When True, HEAD-probe each direct-HTTPS model's latest cycle.

    Returns:
        int: Process exit code (0).
    """
    catalog = Catalog()
    print(f"{len(catalog.datasets)} curated NWP model(s):\n")
    for key, model in catalog.datasets.items():
        cycle = _latest_cycle(model)
        cycle_str = cycle.strftime("%Y-%m-%d %HZ") if cycle else "—"
        line = (
            f"  {key:<14} {model.backend:<14} cycles={model.cycles_utc} "
            f"horizon={model.horizon_h}h latest~{cycle_str}"
        )
        if live and model.backend == "direct-https" and cycle is not None:
            line += f"  [{_probe_direct_https(model, cycle)}]"
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to `sys.argv`).

    Returns:
        int: Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="HEAD-probe direct-HTTPS models for their latest cycle.",
    )
    args = parser.parse_args(argv)
    return summarise(live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
