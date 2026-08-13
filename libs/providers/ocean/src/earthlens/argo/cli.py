"""Catalog-tooling handlers for the Argo backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each Argo dataset family needs a description and a non-empty parameters map.

    Args:
        catalog: The loaded Argo `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, lambda k, r: require(k, r, ("description", "parameters")))
