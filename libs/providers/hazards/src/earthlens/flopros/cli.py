"""Catalog-tooling handlers for the FLOPROS backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """The FLOPROS row needs a URL, shapefile stem, identity columns, and layers.

    The single-shapefile catalog resolves a `layer=` selection against the
    `layers` map and keeps the `identity_columns` on every returned polygon, so
    an emptied map would load cleanly yet break the read — flag it here.

    Args:
        catalog: The loaded FLOPROS `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(
        catalog,
        lambda k, r: require(
            k, r, ("url", "shapefile_stem", "identity_columns", "layers")
        ),
    )
