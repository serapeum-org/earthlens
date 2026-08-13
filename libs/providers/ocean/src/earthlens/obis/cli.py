"""Catalog-tooling handlers for the OBIS backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). OBIS has no anonymous
"list every taxon" endpoint, so the refresh axis is the curated marine-taxa
index; every handler is offline.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import biodiversity_curated_ids, lint, require

#: Curated-id resolver over the combined `available_datasets` + friendly index.
curated_ids = biodiversity_curated_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List OBIS's curated marine-taxa index — the universe IS the catalog.

    Args:
        catalog: The loaded OBIS `Catalog`.

    Returns:
        A single-group mapping `{"obis": [sorted available + friendly ids]}`.
    """
    ids = set(catalog.available_datasets) | set(catalog.datasets)
    return {"obis": sorted(ids)}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report an OBIS curated species' dispatch metadata (offline).

    Args:
        catalog: The loaded OBIS `Catalog`.
        dataset: A curated friendly name (e.g. `"blue-whale"`).

    Returns:
        Single-entry mapping `{dataset: {scientific_name, title}}`.

    Raises:
        ValueError: If `dataset` is not a curated OBIS species.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown OBIS species {dataset!r}")
    return {
        dataset: {
            "scientific_name": record.scientific_name,
            "title": record.title,
        }
    }


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each curated OBIS species needs a scientific name.

    Args:
        catalog: The loaded OBIS `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, lambda k, r: require(k, r, ("scientific_name",)))


def emitter(catalog: Any, upstream_id: str, *, key: str, **opts: Any) -> dict[str, Any]:
    """Seed an OBIS `species:` row from a scientific name (no network).

    Args:
        catalog: The loaded OBIS `Catalog` (unused).
        upstream_id: The OBIS `scientificname` (e.g. `"Mola mola"`).
        key: The friendly catalog key.
        **opts: `title`.

    Returns:
        The seeded row.
    """
    return {
        "scientific_name": upstream_id,
        "title": str(opts.get("title") or key.replace("-", " ").title()),
    }
