"""Audit ``firms_data_catalog.yaml`` against the live FIRMS service.

Diffs the curated catalog against two live sources and flags drift:

* **Sensor availability** — the FIRMS ``data_availability`` endpoint
  (``/api/data_availability/csv/{MAP_KEY}/all``) lists every served
  sensor and its coverage window. The audit reports catalog sensors no
  longer served and live sensors missing from the catalog.
* **Column schema** (with ``--with-columns``) — re-uses the
  ``probe_firms_columns`` probe to sample each catalog sensor and report
  catalog columns absent from the live CSV and live columns missing from
  the catalog ``columns:`` map.

This is the FIRMS analog of ``tools/{gee,chc,ecmwf}/audit_*.py`` and
pairs with ``probe_firms_columns.py``. It reads the catalog through
:class:`earthlens.firms.Catalog`.

Run with::

    pixi run -e dev python tools/firms/audit_firms_catalog.py
    pixi run -e dev python tools/firms/audit_firms_catalog.py --with-columns --strict
    pixi run -e dev python tools/firms/audit_firms_catalog.py --format json

``--strict`` exits non-zero when any drift is found (CI-ready). Needs a
free FIRMS MAP_KEY (``--map-key`` or ``FIRMS_MAP_KEY``). Not part of the
installed package.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from earthlens.firms import Catalog  # noqa: E402
from earthlens.firms._helpers import classify_body, firms_get  # noqa: E402
from probe_firms_columns import DEFAULT_BBOX, probe_sensor  # noqa: E402

DATA_AVAILABILITY_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{map_key}/all"
)


def fetch_live_sensors(map_key: str, timeout: float) -> dict[str, dict[str, Any]]:
    """Return the live sensor coverage from the data_availability endpoint.

    Args:
        map_key: The FIRMS MAP_KEY.
        timeout: Per-request timeout in seconds.

    Returns:
        A map ``{sensor_code: {min_date, max_date}}`` for every served
        sensor.

    Raises:
        RuntimeError: If the endpoint returns a non-CSV body.
    """
    url = DATA_AVAILABILITY_URL.format(map_key=map_key)
    response = firms_get(url, timeout=timeout, get=requests.get)
    text = response.text
    # The data_availability CSV header is `data_id,min_date,max_date` — a
    # different shape from the area endpoint's `latitude,...` header, so
    # `classify_body` (area-specific) is only used to label an *error*
    # body (bad key / quota); a valid response starts with `data_id`.
    if not text.lstrip().lower().startswith("data_id"):
        raise RuntimeError(
            f"data_availability returned a non-CSV body "
            f"({classify_body(text)}): {text[:200]}"
        )
    frame = pd.read_csv(StringIO(text))
    id_col = "data_id" if "data_id" in frame.columns else frame.columns[0]
    live: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        code = str(row[id_col]).strip()
        live[code] = {
            "min_date": _cell(row, "min_date"),
            "max_date": _cell(row, "max_date"),
        }
    return live


def _cell(row: pd.Series, name: str) -> Any:
    """Return a row cell as a string, or ``None`` when absent/null."""
    if name not in row or pd.isna(row[name]):
        return None
    return str(row[name])


def audit(
    map_key: str,
    with_columns: bool,
    bbox: str,
    day_range: int,
    timeout: float,
) -> dict[str, Any]:
    """Compare the catalog against the live service and collect drift.

    Args:
        map_key: The FIRMS MAP_KEY.
        with_columns: When ``True``, also probe each sensor's live CSV
            columns and diff them against the catalog.
        bbox: Sample bbox (``W,S,E,N``) for the column probe.
        day_range: Sample window length for the column probe.
        timeout: Per-request timeout in seconds.

    Returns:
        A report dict with ``sensors`` (per-code status + coverage) and,
        when requested, ``columns`` (per-code column drift), plus a
        boolean ``drift`` flag.
    """
    catalog = Catalog()
    catalog_codes = set(catalog.codes())
    live = fetch_live_sensors(map_key, timeout)
    live_codes = set(live)

    sensors: dict[str, Any] = {}
    for code in sorted(catalog_codes | live_codes):
        if code in catalog_codes and code in live_codes:
            status = "ok"
        elif code in catalog_codes:
            status = "catalog-only (no longer served?)"
        else:
            status = "live-only (missing from catalog)"
        sensors[code] = {"status": status, **live.get(code, {})}

    drift = any(row["status"] != "ok" for row in sensors.values())
    report: dict[str, Any] = {"sensors": sensors}

    if with_columns:
        columns: dict[str, Any] = {}
        for code in sorted(catalog_codes & live_codes):
            catalog_cols = set(catalog.get_sensor(code).columns)
            probed = probe_sensor(code, map_key, bbox, day_range, timeout)
            live_cols = set(probed.get("columns", {}))
            missing_live = sorted(catalog_cols - live_cols) if live_cols else []
            missing_catalog = sorted(live_cols - catalog_cols)
            columns[code] = {
                "note": probed.get("note"),
                "catalog_columns_absent_live": missing_live,
                "live_columns_missing_from_catalog": missing_catalog,
            }
            if missing_live or missing_catalog:
                drift = True
        report["columns"] = columns

    report["drift"] = drift
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """Render the audit report as a Markdown summary.

    Args:
        report: The dict returned by :func:`audit`.

    Returns:
        A Markdown string with a per-sensor table (and a column-drift
        section when columns were probed).
    """
    lines = ["# FIRMS catalog audit", "", "## Sensors", "", "| Sensor | Status | Coverage |", "|---|---|---|"]
    for code, row in report["sensors"].items():
        coverage = f"{row.get('min_date') or '?'} .. {row.get('max_date') or '?'}"
        lines.append(f"| `{code}` | {row['status']} | {coverage} |")
    if "columns" in report:
        lines += ["", "## Column drift", ""]
        for code, row in report["columns"].items():
            absent = ", ".join(row["catalog_columns_absent_live"]) or "-"
            missing = ", ".join(row["live_columns_missing_from_catalog"]) or "-"
            note = f" ({row['note']})" if row.get("note") else ""
            lines.append(f"- `{code}`{note}: catalog-only=[{absent}] live-only=[{missing}]")
    lines += ["", f"**Drift detected:** {report['drift']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: audit the catalog and print the report.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code: 0 when clean (or non-strict), 1 under
        ``--strict`` when drift is found, 2 when no MAP_KEY is available.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-key", default=os.environ.get("FIRMS_MAP_KEY"))
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--with-columns", action="store_true", help="also probe columns.")
    parser.add_argument("--bbox", default=DEFAULT_BBOX)
    parser.add_argument("--day-range", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero when drift is found."
    )
    args = parser.parse_args(argv)

    if not args.map_key:
        parser.error(
            "no MAP_KEY: pass --map-key or set FIRMS_MAP_KEY "
            "(https://firms.modaps.eosdis.nasa.gov/api/map_key/)."
        )

    report = audit(
        args.map_key, args.with_columns, args.bbox, args.day_range, args.timeout
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))

    return 1 if (args.strict and report["drift"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
