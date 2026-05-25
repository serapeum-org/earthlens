"""Audit `tropycal_data_catalog.yaml` against tropycal's supported basins.

Diffs the bundled catalog (loaded via `earthlens.tropycal.Catalog`)
against tropycal's known basin/source universe and, optionally, a probe
sidecar produced by `probe_tropycal_fields.py`:

* basins in the catalog that tropycal no longer serves,
* tropycal basins missing from the catalog,
* `(basin, source)` pairs the catalog declares that tropycal does not
  support,
* (with `--probe`) catalog fields absent from the probe sample, and
  sample fields missing from the catalog.

This is the Tropycal analog of `tools/{gee,chc,ecmwf}/audit_*.py`; it
pairs with `probe_tropycal_fields.py`. It is a maintainer tool, not part
of the installed package. `--strict` exits non-zero on any drift so the
check is CI-ready.

Usage:

    pixi run -e dev python tools/tropycal/audit_tropycal_catalog.py \
        --probe C:/tmp/tropycal_probe/north_atlantic.json --strict
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from earthlens.tropycal import Catalog

#: tropycal 1.4's basin universe and which sources serve each (verified by
#: the A3 spike against the TrackDataset docstring). There is no `jtwc`
#: source. `both` is HURDAT NA+EP; `all` is IBTrACS global.
_SDK_BASIN_SOURCES: dict[str, list[str]] = {
    "north_atlantic": ["ibtracs", "hurdat"],
    "east_pacific": ["ibtracs", "hurdat"],
    "both": ["hurdat"],
    "west_pacific": ["ibtracs"],
    "north_indian": ["ibtracs"],
    "south_indian": ["ibtracs"],
    "australia": ["ibtracs"],
    "south_pacific": ["ibtracs"],
    "south_atlantic": ["ibtracs"],
    "all": ["ibtracs"],
}

#: Catalog fields that `events.py` derives rather than reads from
#: `to_dataframe()`, so their absence from a probe sample is expected, not
#: drift. `category` is computed from `vmax` (Saffir-Simpson).
_DERIVED_FIELDS = frozenset({"category"})


def audit(catalog: Catalog, probe: dict[str, Any] | None) -> dict[str, list[str]]:
    """Compute the catalog-vs-SDK drift report.

    Args:
        catalog: The loaded Tropycal catalog.
        probe: Optional probe sidecar (from `probe_tropycal_fields.py`),
            used to diff the catalog `fields:` against observed columns.

    Returns:
        A mapping `check_name -> sorted list of offenders`. Every empty
        list means that check passes; an all-empty report is clean.
    """
    catalog_basins = set(catalog.codes())
    sdk_basins = set(_SDK_BASIN_SOURCES)

    invalid_pairs: list[str] = []
    for code in sorted(catalog_basins & sdk_basins):
        declared = set(catalog.sources_for(code))
        supported = set(_SDK_BASIN_SOURCES[code])
        for bad in sorted(declared - supported):
            invalid_pairs.append(f"{code}:{bad}")

    report = {
        "basins_not_in_sdk": sorted(catalog_basins - sdk_basins),
        "sdk_basins_missing_from_catalog": sorted(sdk_basins - catalog_basins),
        "invalid_basin_source_pairs": invalid_pairs,
    }

    if probe is not None:
        report.update(_field_drift(catalog, probe))
    return report


def _field_drift(catalog: Catalog, probe: dict[str, Any]) -> dict[str, list[str]]:
    """Diff a probe sidecar's observed fields against the catalog's fields."""
    basin = probe.get("basin")
    observed = set((probe.get("fields") or {}).keys())
    try:
        declared = set(catalog.get_basin(basin).fields)
    except ValueError:
        return {"probe_basin_not_in_catalog": [str(basin)]}
    # Catalog fields are a curated subset (vmax/mslp/category); flag a
    # curated, non-derived field the probe never saw, and note observed
    # columns the catalog omits (informational, not necessarily drift).
    return {
        "catalog_fields_absent_from_sample": sorted(
            (declared - observed) - _DERIVED_FIELDS
        ),
        "sample_fields_absent_from_catalog": sorted(observed - declared),
    }


def format_markdown(report: dict[str, list[str]]) -> str:
    """Render the drift report as a Markdown table."""
    lines = ["| Check | Offenders |", "|---|---|"]
    for check, offenders in report.items():
        cell = ", ".join(offenders) if offenders else "—"
        lines.append(f"| `{check}` | {cell} |")
    return "\n".join(lines)


def has_drift(report: dict[str, list[str]]) -> bool:
    """Return True when any check (other than informational ones) found drift."""
    informational = {"sample_fields_absent_from_catalog"}
    return any(v for k, v in report.items() if k not in informational)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: audit the catalog and report drift.

    Args:
        argv: Optional argument list (defaults to `sys.argv`).

    Returns:
        `0` when clean (or `--strict` not set); `1` when `--strict` and
        drift is found.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--probe", type=Path, default=None, help="probe sidecar JSON to diff fields"
    )
    parser.add_argument(
        "--format", choices=["markdown", "json"], default="markdown", help="output format"
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero when drift is found"
    )
    args = parser.parse_args(argv)

    probe = json.loads(args.probe.read_text(encoding="utf-8")) if args.probe else None
    report = audit(Catalog(), probe)

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_markdown(report))

    if args.strict and has_drift(report):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    raise SystemExit(main())
