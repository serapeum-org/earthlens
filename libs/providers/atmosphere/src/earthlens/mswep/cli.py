"""Catalog-tooling handlers for the MSWEP / MSWX backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). Offline structural lint only.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import require


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated MSWEP / MSWX products.

    Each product needs an analysis `path_template`, a `default_version` that
    is registered, and non-empty `versions` / `variants` / `resolutions` /
    `variables` blocks. A product with forecast variants (MSWX's `Mid` /
    `Long`) must also declare a `forecast_path_template`.

    Args:
        catalog: The loaded MSWEP `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per problem.
    """
    products = catalog.datasets
    issues: list[str] = []
    for key, product in products.items():
        issues.extend(require(key, product, ("path_template", "default_version")))
        for block in ("versions", "variants", "resolutions", "variables"):
            if not getattr(product, block, None):
                issues.append(f"{key}: empty {block}")
        versions = getattr(product, "versions", None) or {}
        default = getattr(product, "default_version", None)
        if default and default not in versions:
            issues.append(f"{key}: default_version {default!r} not in versions")
        variants = getattr(product, "variants", None) or {}
        has_forecast = any(getattr(v, "is_forecast", False) for v in variants.values())
        if has_forecast and not getattr(product, "forecast_path_template", ""):
            issues.append(f"{key}: has forecast variants but no forecast_path_template")
    return len(products), issues
