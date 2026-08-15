"""Catalog-tooling handlers for the ASF backend.

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`).
"""

from __future__ import annotations

from typing import Any


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Every ASF row's PLATFORM/DATASET/PRODUCT_TYPE must exist in `asf_search`.

    The catalog is hand-curated against the SDK's enum modules; if a constant is
    renamed or removed upstream, the row would silently break only at first live
    query. This imports the SDK and checks every row's `platform` / `dataset` /
    `product_type` member name still exists on the matching module.

    Args:
        catalog: The loaded ASF `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per row whose
        constants no longer resolve.
    """
    try:
        import asf_search as asf
    except ImportError:
        return 0, [
            f"asf_search is not installed; install the `asf` extra to "
            f"validate {len(catalog.datasets)} curated row(s)"
        ]
    issues: list[str] = []
    products = catalog.datasets
    for key, row in products.items():
        if row.platform is not None and not hasattr(asf.PLATFORM, row.platform):
            issues.append(f"{key}: PLATFORM.{row.platform} not in asf_search")
        if row.dataset is not None and not hasattr(asf.DATASET, row.dataset):
            issues.append(f"{key}: DATASET.{row.dataset} not in asf_search")
        if not hasattr(asf.PRODUCT_TYPE, row.product_type):
            issues.append(f"{key}: PRODUCT_TYPE.{row.product_type} not in asf_search")
    return len(products), issues
