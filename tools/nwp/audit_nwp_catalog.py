"""Audit the bundled NWP catalog for internal consistency.

A static, offline lint over `src/earthlens/nwp/nwp_data_catalog.yaml`:
it flags rows whose declared `backend` is inconsistent with their other
fields (e.g. a `direct-https` model with no `url_template`, or a Herbie
model with no `model_family`), empty band maps, and out-of-range cycle
hours. It does **not** hit the network — pair it with
`refresh_nwp_catalog.py --live` for availability drift.

Run with:

    pixi run -e dev python tools/nwp/audit_nwp_catalog.py
"""

from __future__ import annotations

import argparse

from earthlens.nwp import Catalog
from earthlens.nwp.catalog import NWPModel


def audit_model(key: str, model: NWPModel) -> list[str]:
    """Return a list of consistency problems for one model row.

    Args:
        key: The model key.
        model: The parsed :class:`NWPModel`.

    Returns:
        list[str]: Human-readable problem descriptions (empty when the
            row is internally consistent).
    """
    problems: list[str] = []
    if not model.bands:
        problems.append("no bands declared")
    if not model.cycles_utc:
        problems.append("no cycles_utc declared")
    if any(not 0 <= h <= 23 for h in model.cycles_utc):
        problems.append(f"cycle hour out of range: {model.cycles_utc}")
    if model.horizon_h < 0:
        problems.append("horizon_h must be non-negative (0 is valid for analyses)")
    if model.backend == "direct-https" and not model.url_template:
        problems.append("direct-https model missing url_template")
    if model.backend == "herbie" and not model.model_family:
        problems.append("herbie model missing model_family")
    return [f"{key}: {p}" for p in problems]


def audit() -> int:
    """Audit every curated model; print problems and return an exit code.

    Returns:
        int: 0 when the catalog is clean, 1 when any problem was found.
    """
    catalog = Catalog()
    problems: list[str] = []
    for key, model in catalog.datasets.items():
        problems.extend(audit_model(key, model))
    if problems:
        print(f"{len(problems)} catalog problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK — {len(catalog.datasets)} model(s), no consistency problems.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to `sys.argv`).

    Returns:
        int: Process exit code (0 clean, 1 problems found).
    """
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return audit()


if __name__ == "__main__":
    raise SystemExit(main())
