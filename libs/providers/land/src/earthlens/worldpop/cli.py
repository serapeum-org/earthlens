"""Catalog-tooling handlers for the WorldPop backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._land_cli`). The refresher / prober use the
public WorldPop REST hub; `--write` persists the full live product universe to a
sibling `available_products.yaml` (the catalog's `available_*` is computed from
the curated rows at load time).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import BackendInfo, get_json, write_sibling_index

_REST_URL = "https://hub.worldpop.org/rest/data"


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List WorldPop sub-alias ids per product alias, live (public REST).

    Args:
        _catalog: The loaded WorldPop `Catalog` (unused; the REST is the source).

    Returns:
        A mapping of product alias to its sorted sub-alias ids.
    """
    top = get_json(_REST_URL).get("data", [])
    grouped: dict[str, list[str]] = {}
    for entry in top:
        alias = entry.get("alias")
        if not alias:
            continue
        rows = get_json(f"{_REST_URL}/{alias}").get("data", [])
        grouped[str(alias)] = sorted(
            {sub for row in rows if (sub := str(row.get("alias", "")).strip())}
        )
    return grouped


def curated_ids(catalog: Any) -> list[str]:
    """Return the sub-alias ids the WorldPop catalog curates (flattened)."""
    return sorted(
        {
            sid
            for record in catalog.datasets.values()
            for sub in (getattr(record, "subaliases", None) or [])
            if (sid := getattr(sub, "id", None))
        }
    )


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite WorldPop's sibling `available_products.yaml` (alias -> sub-aliases)."""
    return write_sibling_index(
        info, "available_products.yaml", {"available_products": grouped}
    )


def _resolve(catalog: Any, dataset: str) -> tuple[str, str]:
    """Resolve `dataset` to a `(product_alias, sub_alias)` pair.

    Accepts a product alias (uses its first sub-alias) or a sub-alias id
    (finds its parent product).

    Raises:
        ValueError: If `dataset` matches no product or sub-alias.
    """
    record = catalog.datasets.get(dataset)
    if record is not None:
        subs = getattr(record, "subaliases", None) or []
        if subs:
            return dataset, getattr(subs[0], "id", dataset)
    for alias, row in catalog.datasets.items():
        for sub in getattr(row, "subaliases", None) or []:
            if getattr(sub, "id", None) == dataset:
                return alias, dataset
    raise ValueError(f"no WorldPop product or sub-alias matches {dataset!r}")


def _records(alias: str, sub_alias: str, iso3: str) -> list[dict[str, Any]]:
    """Return the live WorldPop records for one `(alias, sub_alias, iso3)`."""
    from earthlens.worldpop.rest import rest_records

    return rest_records(alias, sub_alias, iso3)


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a WorldPop sub-alias's live REST record shape (public).

    Args:
        catalog: The loaded WorldPop `Catalog` (resolves the product alias).
        dataset: A product alias or a sub-alias id.

    Returns:
        Mapping of record field name to `{dtype}` (`popyears` carries the
        sampled year spread).
    """
    alias, sub_alias = _resolve(catalog, dataset)
    records = _records(alias, sub_alias, "USA")
    if not records:
        return {}
    schema: dict[str, dict[str, Any]] = {
        field: {"dtype": type(value).__name__} for field, value in records[0].items()
    }
    schema["popyears"] = {
        "dtype": "list",
        "values": sorted({str(r.get("popyear")) for r in records if r.get("popyear")}),
    }
    return schema


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Structural lint of the curated WorldPop products (via `Catalog.health()`).

    Args:
        catalog: The loaded WorldPop `Catalog`.

    Returns:
        `(checked, issues)` — the product count and one message per offender.
    """
    issues = [
        f"{offender}: {check}"
        for check, offenders in catalog.health().items()
        for offender in offenders
    ]
    return len(catalog.datasets), issues
