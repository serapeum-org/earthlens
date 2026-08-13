"""Catalog-tooling handlers for the NREL backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each NREL product needs a source, a CSV endpoint, and non-empty columns."""
    return lint(catalog, lambda k, r: require(k, r, ("source", "endpoint", "columns")))
