"""Download the result of every successful CDS probe in the user's
recent job list and extract its NetCDF metadata.

Pairs with `tools.probe_open_datasets` — that script submits
fire-and-forget requests; this one waits for them to finish and
pulls the resulting NetCDF / Zip into `C:/tmp/cds_probe/` before
running nc-variable extraction.

Thin CLI wrapper around `Catalog.list_recent_jobs` and
`Catalog.download_job` from the package — no duplicated HTTP
plumbing here.

Usage::

    pixi run -e dev python tools/ecmwf/download_probe_results.py
    pixi run -e dev python tools/ecmwf/download_probe_results.py --max-age-min 60
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any

from pyramids.netcdf import NetCDF

from earthlens.ecmwf import Catalog

CACHE_DIR = Path("C:/tmp/cds_probe")


def maybe_unzip(nc_path: Path) -> Path:
    """If CDS returned a zip wrapping NetCDFs, unzip and return the dir."""
    extracted = nc_path.with_suffix(".extracted")
    if zipfile.is_zipfile(nc_path):
        if not extracted.exists():
            extracted.mkdir()
            with zipfile.ZipFile(nc_path) as zf:
                zf.extractall(extracted)  # nosec B202 — trusted CDS payload
        return extracted
    return nc_path


def collect_metadata(path: Path) -> dict[str, dict[str, str]]:
    """Walk ``path`` (file or dir) and collect long_name + units per nc var."""
    files = sorted(path.glob("*.nc")) if path.is_dir() else [path]
    skip = {"latitude", "longitude", "time", "valid_time", "number", "expver"}
    out: dict[str, dict[str, str]] = {}
    for nc in files:
        with NetCDF.read_file(str(nc), read_only=True) as fh:
            for name, var in fh.meta_data.variables.items():
                if name in skip:
                    continue
                long_name = getattr(var, "long_name", "") or ""
                units = getattr(var, "unit", "") or ""
                if long_name or units:
                    out[name] = {"long_name": long_name, "units": units}
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-min", type=int, default=60)
    parser.add_argument(
        "--out-json", type=Path, default=CACHE_DIR / "_open_summary.json"
    )
    args = parser.parse_args()
    cat = Catalog()
    jobs = cat.list_recent_jobs(
        status="successful", max_age_min=args.max_age_min, limit=100
    )
    print(f"Found {len(jobs)} successful job(s) within last {args.max_age_min}m")
    summary: dict[str, dict[str, Any]] = {}
    for job in jobs:
        process = job["processID"]
        target = CACHE_DIR / f"{process}_open.nc"
        try:
            path = cat.download_job(job["jobID"], target)
            extracted = maybe_unzip(path)
            metadata = collect_metadata(extracted)
            summary[process] = {
                "jobID": job["jobID"][:8],
                "path": str(path),
                "nc_variables": metadata,
            }
            print(f"  [OK] {process}: {len(metadata)} nc_variable(s)")
        except Exception as exc:
            print(f"  [ERR] {process}: {type(exc).__name__}: {str(exc)[:100]}")
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote summary to {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
