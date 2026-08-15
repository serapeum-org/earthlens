"""Catalog-tooling handlers for the RADKLIM / RADOLAN backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def _check_radklim_row(key: str, record: Any) -> list[str]:
    """Lint one RADKLIM product: stream fields + a served default format.

    A `reproc` product must carry the reprocessing `version` and the CDC path
    token `cdc_frequency`; an `operational` product must carry a positive
    `retention_days`; and `default_format` must be one of the row's `formats`.

    Args:
        key: The product id.
        record: The `earthlens.radklim.RadklimProduct` row.

    Returns:
        One issue string per problem found.
    """
    issues = require(key, record, ("stream", "code", "default_format", "formats"))
    stream = getattr(record, "stream", None)
    if stream == "reproc":
        issues += require(key, record, ("version", "cdc_frequency"))
    elif stream == "operational" and not getattr(record, "retention_days", 0):
        issues.append(f"{key}: operational row needs a positive retention_days")
    fmt = getattr(record, "default_format", None)
    formats = getattr(record, "formats", None) or []
    if fmt and formats and fmt not in formats:
        issues.append(f"{key}: default_format {fmt!r} not in formats {formats}")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each RADKLIM / RADOLAN product needs its stream fields + a served format."""
    return lint(catalog, _check_radklim_row)
