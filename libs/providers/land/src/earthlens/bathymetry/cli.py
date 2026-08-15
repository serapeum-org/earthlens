"""Catalog-tooling handlers for the bathymetry backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each bathymetry DEM row needs an endpoint, coverage id, and band.

    The lint additionally flags any curated id missing from the bundled
    `available_datasets:` index, which a hand-edit could desync.

    Args:
        catalog: The loaded bathymetry `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    available = set(catalog.available_datasets or ())
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        issues.extend(require(key, row, ("endpoint", "dataset_id", "variable")))
        if available and key not in available:
            issues.append(f"{key}: id not in the bundled `available_datasets:` index")
    return len(catalog.datasets), issues
