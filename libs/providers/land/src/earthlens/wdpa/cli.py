"""Catalog-tooling handlers for the WDPA (Protected Planet) backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`). Protected Planet is token-gated,
so the refresh axis is the curated ISO3 country set and every handler is offline.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import biodiversity_curated_ids, lint, require

#: Curated-id resolver over the combined `available_datasets` + friendly index.
curated_ids = biodiversity_curated_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List the WDPA country axis — the universe IS the curated ISO3 set.

    Args:
        catalog: The loaded WDPA `Catalog`.

    Returns:
        A single-group mapping `{"wdpa": [sorted ISO3 codes]}`.
    """
    return {"wdpa": sorted(set(catalog.available_datasets) | set(catalog.datasets))}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report a WDPA curated country's dispatch metadata (offline).

    Args:
        catalog: The loaded WDPA `Catalog`.
        dataset: A curated ISO3 alpha-3 code (e.g. `"KEN"`).

    Returns:
        Single-entry mapping `{dataset: {name, region}}`.

    Raises:
        ValueError: If `dataset` is not a curated WDPA country.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown WDPA country {dataset!r}")
    return {dataset: {"name": record.name, "region": record.region}}


def _check_country(key: str, record: Any) -> list[str]:
    """Flag a WDPA country missing a name or with a malformed ISO3 key."""
    issues = require(key, record, ("name",))
    if not (len(key) == 3 and key.isalpha() and key.isupper()):
        issues.append(f"{key}: catalog key must be an upper-case ISO3 alpha-3 code")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each curated WDPA country needs a name and an ISO3 alpha-3 key.

    Args:
        catalog: The loaded WDPA `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, _check_country)


def emitter(catalog: Any, upstream_id: str, *, key: str, **opts: Any) -> dict[str, Any]:
    """Seed a WDPA `countries:` row from an ISO3 code (no network).

    Args:
        catalog: The loaded WDPA `Catalog` (unused).
        upstream_id: The ISO3 alpha-3 code (e.g. `"KEN"`).
        key: The friendly catalog key (typically the same alpha-3 code).
        **opts: `name`, `region`.

    Returns:
        The seeded row.
    """
    return {
        "name": str(opts.get("name") or key),
        "region": str(opts.get("region") or ""),
    }
