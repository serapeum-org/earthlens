"""Catalog-tooling handlers for the glaciers backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def _row_issues(key: str, record: Any) -> list[str]:
    """Lint one glaciers row: common fields + per-source request detail."""
    issues = require(key, record, ("source", "output_kind", "long_name", "citation"))
    source = getattr(record, "source", None)
    if source == "wgms":
        issues += require(key, record, ("table", "archive_url"))
    elif source == "glims":
        issues += require(key, record, ("wfs_url", "wfs_typename"))
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each glaciers row needs a source + output kind + the per-source detail.

    Args:
        catalog: The loaded glaciers `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, _row_issues)
