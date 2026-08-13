"""Catalog-tooling handlers for the IUCN (Red List) backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`). The Red List API is token-gated,
so the refresh axis is the curated ISO2 country set and every handler is offline.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import biodiversity_curated_ids, lint, require

#: Curated-id resolver over the combined `available_datasets` + friendly index.
curated_ids = biodiversity_curated_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List the IUCN country axis — the universe IS the curated ISO2 set.

    Args:
        catalog: The loaded IUCN `Catalog`.

    Returns:
        A single-group mapping `{"iucn": [sorted ISO2 codes]}`.
    """
    return {"iucn": sorted(set(catalog.available_datasets) | set(catalog.datasets))}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Report an IUCN curated country's dispatch metadata (offline).

    Args:
        catalog: The loaded IUCN `Catalog`.
        dataset: A curated ISO2 alpha-2 code (e.g. `"KE"`).

    Returns:
        Single-entry mapping `{dataset: {name, region}}`.

    Raises:
        ValueError: If `dataset` is not a curated IUCN country.
    """
    record = catalog.datasets.get(dataset)
    if record is None:
        raise ValueError(f"unknown IUCN country {dataset!r}")
    return {dataset: {"name": record.name, "region": record.region}}


def _check_country(key: str, record: Any) -> list[str]:
    """Flag an IUCN country missing a name or with a malformed ISO2 key."""
    issues = require(key, record, ("name",))
    if not (len(key) == 2 and key.isalpha() and key.isupper()):
        issues.append(f"{key}: catalog key must be an upper-case ISO2 alpha-2 code")
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each curated IUCN country needs a name and an ISO2 alpha-2 key.

    Args:
        catalog: The loaded IUCN `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    return lint(catalog, _check_country)


def emitter(catalog: Any, upstream_id: str, *, key: str, **opts: Any) -> dict[str, Any]:
    """Seed an IUCN `countries:` row from an ISO2 code (no network).

    Args:
        catalog: The loaded IUCN `Catalog` (unused).
        upstream_id: The ISO2 alpha-2 code (e.g. `"KE"`).
        key: The friendly catalog key (typically the same alpha-2 code).
        **opts: `name`, `region`.

    Returns:
        The seeded row.
    """
    return {
        "name": str(opts.get("name") or key),
        "region": str(opts.get("region") or ""),
    }
