"""Catalog-tooling handlers for the Aqueduct backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each admin level needs a zip + shapefile stem, and every vocab is populated.

    Beyond the admin-level rows, the request path resolves a column name from the
    `indicators` / `years` / `scenarios` / `return_periods` vocabularies, so an
    emptied vocabulary would load cleanly yet break every download — flag it here.

    Args:
        catalog: The loaded Aqueduct `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    checked, issues = lint(
        catalog, lambda k, r: require(k, r, ("zip", "shapefile_stem"))
    )
    for vocabulary in ("indicators", "years", "scenarios", "return_periods"):
        if not getattr(catalog, vocabulary, None):
            issues.append(f"catalog: the {vocabulary!r} vocabulary is empty")
    return checked, issues
