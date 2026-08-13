"""Catalog-tooling handlers for the OSM backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def _check_row(key: str, record: Any) -> list[str]:
    """Flag an OSM query missing protocol/geometry, or its protocol's query field."""
    issues = require(key, record, ("protocol", "geometry_types"))
    protocol = getattr(record, "protocol", None)
    if protocol == "overpass" and not getattr(record, "query_template", None):
        issues.append(f"{key}: overpass row missing query_template")
    if protocol == "ohsome" and not getattr(record, "ohsome_filter", None):
        issues.append(f"{key}: ohsome row missing ohsome_filter")
    if protocol == "pbf" and not getattr(record, "pyrosm_method", None):
        issues.append(f"{key}: pbf row missing pyrosm_method")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each OSM named query needs a protocol and that protocol's query field.

    Args:
        catalog: The loaded OSM `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, _check_row)
