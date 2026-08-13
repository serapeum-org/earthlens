"""Catalog-tooling handlers for the EM-DAT backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each EM-DAT dataset needs the prose its row model does not already force.

    `long_name` and `licence` are required by the pydantic row model, and
    `hazard_vocabulary` has a default the loader then checks against the
    `hazard_vocabularies:` block — so a catalog missing any of them never loads
    far enough to reach validation. `description` and `citation` are the
    genuinely optional fields worth insisting on.

    Args:
        catalog: The loaded EM-DAT `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, lambda k, r: require(k, r, ("description", "citation")))
