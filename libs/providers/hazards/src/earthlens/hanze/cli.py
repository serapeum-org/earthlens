"""Catalog-tooling handlers for the HANZE backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each HANZE flood type needs a description; the record/geometry stay pinned.

    Beyond the per-flood-type lint, the single-product HANZE catalog carries a
    pinned Zenodo record, a file map, the region-geometry join and the
    friendly-to-header column map at the top level; a stanza that dropped any of
    those would still load, so they are checked here rather than left to the row
    model. The column keys are the ones the backend's `_filter_events` reads, so a
    catalog missing them passes structural load but breaks at fetch time.

    Args:
        catalog: The loaded HANZE `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    checked, issues = lint(catalog, lambda k, r: require(k, r, ("description",)))
    record = getattr(catalog, "record", None)
    if record is None or not getattr(record, "record", 0):
        issues.append("record: missing pinned Zenodo record id")
    geometry = getattr(catalog, "geometry", None)
    if geometry is None or not getattr(geometry, "member_stem", ""):
        issues.append("geometry: missing shapefile member_stem")
    files = getattr(catalog, "files", None) or {}
    for required in ("events", "regions"):
        if required not in files:
            issues.append(f"files: missing required file {required!r}")
    columns = getattr(catalog, "columns", None) or {}
    for required in ("country_code", "type", "year", "regions_nuts3"):
        if required not in columns:
            issues.append(f"columns: missing required key {required!r}")
    return checked, issues
