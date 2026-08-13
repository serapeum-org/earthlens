"""Catalog-tooling handlers for the GOES backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated GOES ABI products.

    Every product needs a `product_group` and a non-empty `domains` list
    whose entries are all known domain keys, a `default_domain` drawn from
    that list, and — for a band-split product — a non-empty `bands` list.

    Args:
        catalog: The loaded GOES `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    known_domains = set(catalog.domains)
    issues: list[str] = []
    for key, product in catalog.datasets.items():
        issues.extend(require(key, product, ("product_group", "domains")))
        domains = getattr(product, "domains", None) or []
        unknown = [d for d in domains if d not in known_domains]
        if unknown:
            issues.append(f"{key}: unknown domain(s) {unknown}")
        default = getattr(product, "default_domain", None)
        if domains and default not in domains:
            issues.append(f"{key}: default_domain {default!r} not in domains {domains}")
        if getattr(product, "band_split", False) and not getattr(
            product, "bands", None
        ):
            issues.append(f"{key}: band_split product needs a non-empty bands list")
    return len(catalog.datasets), issues
