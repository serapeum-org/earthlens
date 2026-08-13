"""Catalog-tooling handlers for the GBIF backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`). GBIF has no anonymous
"list every taxon" endpoint, so the refresh axis is the curated taxa index;
every handler is offline.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import biodiversity_curated_ids, lint, require

#: Curated-id resolver over the combined `available_datasets` + friendly index.
curated_ids = biodiversity_curated_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List GBIF's curated biodiversity index — the universe IS the catalog.

    Args:
        catalog: The loaded GBIF `Catalog`.

    Returns:
        A single-group mapping `{"gbif": [sorted available + friendly ids]}`.
    """
    ids = set(catalog.available_datasets) | set(catalog.datasets)
    return {"gbif": sorted(ids)}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report a GBIF curated taxon's dispatch metadata (offline).

    Args:
        catalog: The loaded GBIF `Catalog`.
        dataset: A curated friendly name (e.g. `"birds"`).

    Returns:
        Single-entry mapping `{dataset: {taxon_key, title, rank}}`.

    Raises:
        ValueError: If `dataset` is not a curated GBIF taxon.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown GBIF taxon {dataset!r}")
    return {
        dataset: {
            "taxon_key": record.taxon_key,
            "title": record.title,
            "rank": record.rank,
        }
    }


def _check_taxon(key: str, record: Any) -> list[str]:
    """Flag a GBIF taxon missing its key or carrying a non-positive one."""
    issues = require(key, record, ("taxon_key",))
    taxon_key = getattr(record, "taxon_key", None)
    if taxon_key is not None and taxon_key <= 0:
        issues.append(f"{key}: taxon_key must be positive, got {taxon_key!r}")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each curated GBIF taxon needs a positive integer backbone taxonKey.

    Args:
        catalog: The loaded GBIF `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, _check_taxon)


def emitter(catalog: Any, upstream_id: str, *, key: str, **opts: Any) -> dict[str, Any]:
    """Seed a GBIF `taxa:` row from a backbone `taxonKey` (no network).

    Args:
        catalog: The loaded GBIF `Catalog` (unused).
        upstream_id: The GBIF backbone `taxonKey` (digit string or integer).
        key: The friendly catalog key.
        **opts: `title`, `rank`.

    Returns:
        The seeded row.
    """
    return {
        "taxon_key": int(upstream_id),
        "title": str(opts.get("title") or key.replace("-", " ").title()),
        "rank": str(opts.get("rank") or ""),
    }
