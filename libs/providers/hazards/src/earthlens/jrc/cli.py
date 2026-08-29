"""Catalog-tooling handlers for the JRC flood (EFHM) backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each EFHM row needs a band, base URL, filename template, and return periods.

    Args:
        catalog: The loaded JRC-flood `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(
        catalog,
        lambda k, r: require(
            k, r, ("band", "base_url", "filename_template", "return_periods")
        ),
    )
