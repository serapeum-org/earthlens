"""Catalog-tooling handlers for the SoilGrids backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from earthlens.cli.toolkit import lint, require


def _row_issues(key: str, record: Any) -> list[str]:
    """Lint one soilgrids property: WCS endpoint, depths, quantiles + `mean`."""
    issues = require(key, record, ("endpoint", "depths", "quantiles"))
    endpoint = getattr(record, "endpoint", "") or ""
    # Compare the parsed host exactly, not a substring — a substring check would
    # accept a spoofed host like `maps.isric.org.example.com`.
    if endpoint and urlsplit(endpoint).hostname != "maps.isric.org":
        issues.append(f"{key}: endpoint host is not maps.isric.org")
    quantiles = getattr(record, "quantiles", None) or []
    if quantiles and "mean" not in quantiles:
        issues.append(f"{key}: quantiles missing the default 'mean' layer")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each soilgrids property needs a WCS endpoint, depths, and quantiles.

    Args:
        catalog: The loaded SoilGrids `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, _row_issues)
