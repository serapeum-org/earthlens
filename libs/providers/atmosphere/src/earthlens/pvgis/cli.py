"""Catalog-tooling handlers for the PVGIS backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each PVGIS product needs a tool, an endpoint, and non-empty columns."""
    return lint(catalog, lambda k, r: require(k, r, ("tool", "endpoint", "columns")))
