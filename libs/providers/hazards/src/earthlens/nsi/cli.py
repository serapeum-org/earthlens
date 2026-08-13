"""Catalog-tooling handlers for the NSI backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each NSI source needs a provider, endpoint, output kind, and field map.

    Args:
        catalog: The loaded NSI `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(
        catalog,
        lambda k, r: require(k, r, ("provider", "endpoint", "output_kind", "fields")),
    )
