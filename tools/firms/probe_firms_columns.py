"""Probe NASA FIRMS sensors to discover their live CSV column schema.

Fetches a tiny sample per sensor from the FIRMS area CSV API, records the
**actual columns + dtypes** each sensor emits — especially the confidence
shape that varies by family (MODIS numeric 0-100 vs VIIRS categorical
``l``/``n``/``h``) — and writes a JSON sidecar that seeds / verifies the
``columns:`` maps in ``firms_data_catalog.yaml``.

This is the FIRMS analog of ``tools/ecmwf/probe_cds_netcdf.py``. It uses
only the core ``requests`` + ``pandas`` dependencies and the same
URL/auth/body-classification helpers the backend uses, so a probe result
matches what the backend would parse.

Usage::

    pixi run -e dev python tools/firms/probe_firms_columns.py \
        --bbox -125,32,-114,42 --day-range 5 \
        --out C:/tmp/firms_probe/columns.json

Needs a free FIRMS MAP_KEY (``--map-key`` or the ``FIRMS_MAP_KEY`` env
var). Raw CSV samples are cached under ``C:/tmp/firms_probe/<sensor>.csv``
so re-running avoids re-spending transactions. A sensor with no
detections in the sample window is tolerated (recorded with an empty
column map and a note).
"""

from __future__ import annotations

import argparse
import json
import os
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from earthlens.firms import Catalog
from earthlens.firms._helpers import classify_body, firms_get
from earthlens.firms.backend import AREA_URL_TEMPLATE

CACHE_DIR = Path("C:/tmp/firms_probe")

#: A fire-prone default bbox (US West Coast) likely to carry detections.
DEFAULT_BBOX = "-125,32,-114,42"


def probe_sensor(
    sensor: str,
    map_key: str,
    bbox: str,
    day_range: int,
    timeout: float,
) -> dict[str, Any]:
    """Fetch a sample for one sensor and record its column schema.

    Args:
        sensor: FIRMS source code (e.g. ``"VIIRS_SNPP_NRT"``).
        map_key: The FIRMS MAP_KEY.
        bbox: Bounding box as ``W,S,E,N``.
        day_range: Sample window length in days (≤5).
        timeout: Per-request timeout in seconds.

    Returns:
        A record with ``n_rows`` and a ``columns`` map of
        ``{column: {dtype, example}}``; on a non-CSV response a ``note``
        field carries the body kind.
    """
    url = AREA_URL_TEMPLATE.format(
        map_key=map_key,
        sensor=sensor,
        bbox=bbox,
        day_range=day_range,
        start_date="",
    ).rstrip("/")
    response = firms_get(url, timeout=timeout, get=requests.get)
    text = response.text
    kind = classify_body(text)
    if kind != "csv":
        return {"note": f"non-CSV response classified as {kind!r}", "columns": {}}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{sensor}.csv").write_text(text, encoding="utf-8")
    frame = pd.read_csv(StringIO(text))
    columns = {
        column: {
            "dtype": str(frame[column].dtype),
            "example": _first_example(frame[column]),
        }
        for column in frame.columns
    }
    return {"n_rows": int(len(frame)), "columns": columns}


def _first_example(series: pd.Series) -> Any:
    """Return the first non-null value of a column as a JSON-safe scalar.

    Args:
        series: One CSV column.

    Returns:
        The first non-null value coerced to a Python scalar, or ``None``
        when the column is empty / all-null.
    """
    non_null = series.dropna()
    if non_null.empty:
        return None
    value = non_null.iloc[0]
    return value.item() if hasattr(value, "item") else value


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: probe each requested sensor and write the sidecar.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code (0 on success, 2 when no MAP_KEY is available).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sensors",
        default=",".join(Catalog().codes()),
        help="Comma-separated FIRMS sensor codes (default: every catalog sensor).",
    )
    parser.add_argument("--bbox", default=DEFAULT_BBOX, help="bbox as W,S,E,N.")
    parser.add_argument("--day-range", type=int, default=5, help="sample days (<=5).")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--map-key", default=os.environ.get("FIRMS_MAP_KEY"))
    parser.add_argument(
        "--out",
        type=Path,
        default=CACHE_DIR / "columns.json",
        help="JSON sidecar output path.",
    )
    args = parser.parse_args(argv)

    if not args.map_key:
        parser.error(
            "no MAP_KEY: pass --map-key or set FIRMS_MAP_KEY "
            "(https://firms.modaps.eosdis.nasa.gov/api/map_key/)."
        )

    results: dict[str, Any] = {}
    for sensor in [s.strip() for s in args.sensors.split(",") if s.strip()]:
        print(f"Probing {sensor} ...")
        record = probe_sensor(
            sensor, args.map_key, args.bbox, args.day_range, args.timeout
        )
        results[sensor] = record
        if record.get("note"):
            summary = record["note"]
        else:
            summary = f"{record['n_rows']} row(s), {len(record['columns'])} columns"
        print(f"  -> {summary}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
