"""Audit `worldpop_data_catalog.yaml` against the live WorldPop REST API.

Diffs the bundled catalog (loaded via `earthlens.worldpop.Catalog`) against
the hub's live product/sub-alias universe (`hub.worldpop.org/rest/data`):

* curated products the hub no longer exposes as a top-level alias,
* hub products missing from the catalog (excluding the deliberately
  out-of-scope specialty families, see `EXPECTED_UNCURATED`),
* curated `(product, sub-alias)` ids the hub no longer serves.

This is the WorldPop analog of `tools/{tropycal,chc,ecmwf,gee}/audit_*.py`;
it pairs with `probe_worldpop_rest.py`. It is a maintainer tool, not part of
the installed package. `--strict` exits non-zero on any drift so the check
is CI-ready.

Usage:
    python tools/worldpop/audit_worldpop_catalog.py --strict
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

import requests

from earthlens.worldpop import Catalog
from earthlens.worldpop.rest import BASE_URL

Getter = Callable[..., requests.Response]

#: Live top-level aliases earthlens deliberately does NOT curate (specialty /
#: heterogeneous families), so their absence from the catalog is expected, not
#: drift. `covariates` is the 50+ named-layer family; the rest are
#: bespoke/auxiliary products out of the population-grid scope.
EXPECTED_UNCURATED: frozenset[str] = frozenset(
    {
        "covariates",
        "adminareas",
        "dahi",
        "internal_migration_f",
        "dynamic_mapping",
        "global_flight_data",
        "gridcellsurfaceareas",
    }
)


def _live_aliases(*, base_url: str = BASE_URL, get: Getter = requests.get) -> set[str]:
    """Return the hub's live top-level product aliases."""
    resp = get(base_url, timeout=60)
    resp.raise_for_status()
    return {
        str(row.get("alias")).strip()
        for row in resp.json().get("data", [])
        if str(row.get("alias")).strip()
    }


def _live_subaliases(
    alias: str, *, base_url: str = BASE_URL, get: Getter = requests.get
) -> set[str]:
    """Return the hub's live sub-alias ids for one product alias."""
    resp = get(f"{base_url}/{alias}", timeout=60)
    resp.raise_for_status()
    return {
        str(row.get("alias")).strip()
        for row in resp.json().get("data", [])
        if str(row.get("alias")).strip()
    }


def audit(
    catalog: Catalog, *, base_url: str = BASE_URL, get: Getter = requests.get
) -> dict[str, list[str]]:
    """Compute the catalog-vs-live drift report.

    Args:
        catalog: The loaded WorldPop catalog.
        base_url: REST base URL (overridable for tests).
        get: HTTP getter (inject a fake for offline tests).

    Returns:
        A mapping `check_name -> sorted list of offenders`. Every empty list
        means that check passes; an all-empty report is clean.
    """
    curated = set(catalog.available_products())
    live = _live_aliases(base_url=base_url, get=get)

    missing_upstream = sorted(curated - live)
    not_curated = sorted(live - curated - EXPECTED_UNCURATED)

    subalias_drift: list[str] = []
    for product in sorted(curated & live):
        live_ids = _live_subaliases(product, base_url=base_url, get=get)
        for sub in catalog.get(product).subaliases:
            if sub.id not in live_ids:
                subalias_drift.append(f"{product}:{sub.id}")

    return {
        "catalog_products_missing_upstream": missing_upstream,
        "upstream_products_not_curated": not_curated,
        "subalias_missing_upstream": sorted(subalias_drift),
    }


def has_drift(report: dict[str, list[str]]) -> bool:
    """Return whether any check in `report` lists an offender."""
    return any(offenders for offenders in report.values())


def main(argv: list[str] | None = None) -> int:
    """Run the audit CLI; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero on any drift"
    )
    args = parser.parse_args(argv)

    report = audit(Catalog())
    for check, offenders in report.items():
        print(f"{check}: {offenders}")
    drift = has_drift(report)
    print(f"\n{'DRIFT' if drift else 'clean'}.")
    return 1 if (drift and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
