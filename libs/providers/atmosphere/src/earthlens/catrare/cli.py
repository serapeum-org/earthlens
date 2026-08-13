"""Catalog-tooling handlers for the CatRaRE backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each CatRaRE threshold needs a code; the shared version/CRS/columns stay pinned.

    Beyond the per-threshold lint, the catalog composes the download URL and the
    FileGDB layer name from `base_url` / `version` / `version_tag` / `years` and
    reprojects the geometry from `source_crs`; the date filter and the returned
    columns read `date_columns` / `event_columns` / `geometry_layers`. A stanza
    dropping any of those loads cleanly but breaks at fetch time.
    """
    checked, issues = lint(catalog, lambda k, r: require(k, r, ("threshold",)))
    for attr in ("base_url", "version", "version_tag", "years", "source_crs"):
        if not getattr(catalog, attr, ""):
            issues.append(f"catalog: missing {attr!r}")
    for mapping in ("geometry_layers", "date_columns", "event_columns"):
        if not getattr(catalog, mapping, None):
            issues.append(f"catalog: the {mapping!r} is empty")
    return checked, issues
