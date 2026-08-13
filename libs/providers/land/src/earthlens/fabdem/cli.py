"""Catalog-tooling handlers for the FABDEM backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each FABDEM row needs a band and a data version (for the tile URLs).

    Args:
        catalog: The loaded FABDEM `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, lambda k, r: require(k, r, ("band", "version")))
