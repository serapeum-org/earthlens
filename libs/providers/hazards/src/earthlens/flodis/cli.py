"""Catalog-tooling handlers for the FLODIS backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each FLODIS table needs a description and join keys; the record stays pinned.

    Beyond the per-table lint, the two-product FLODIS catalog carries a pinned
    Zenodo record and the friendly-to-header column map at the top level; a stanza
    that dropped either would still load, so they are checked here rather than left
    to the row model. The `iso3`/`year` column keys are the ones the backend's
    `_filter_table` reads, so a catalog missing them passes structural load but
    breaks at fetch time.

    Args:
        catalog: The loaded FLODIS `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    checked, issues = lint(
        catalog, lambda k, r: require(k, r, ("file", "description", "key_columns"))
    )
    record = getattr(catalog, "record", None)
    if record is None or not getattr(record, "record", 0):
        issues.append("record: missing pinned Zenodo record id")
    columns = getattr(catalog, "columns", None) or {}
    for required in ("iso3", "year", "disasterno", "gid_1", "gid_2"):
        if required not in columns:
            issues.append(f"columns: missing required key {required!r}")
    return checked, issues
