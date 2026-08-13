"""Catalog-tooling handlers for the Sentinel Hub backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). Listing collections and probing
their bands reads the `sentinelhub` SDK's `DataCollection` registry offline (no
auth); data *access* is what needs CDSE OAuth.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import curated_attr_ids, index_writer

#: Curated-id resolver over the catalog's `sh_collection` column (audit axis).
curated_ids = curated_attr_ids("sh_collection")

#: Persist a live fetch back into the bundled `available_collections` index.
writer = index_writer("available_collections")


def _sh_data_collection_names() -> list[str]:
    """Return the `sentinelhub.DataCollection` enum member names (no auth)."""
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    return [member.name for member in import_sentinelhub().DataCollection]


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List Sentinel Hub collections from the SDK's `DataCollection` enum.

    Listing the supported collections needs no credentials — it is the
    sentinelhub SDK's authoritative registry (the same source the bundled
    `available_collections` index was built from); data *access* is what
    needs CDSE OAuth.

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (unused; the SDK is the
            source).

    Returns:
        A single-group mapping `{"sentinel_hub": [sorted collection names]}`.
    """
    return {"sentinel_hub": sorted(set(_sh_data_collection_names()))}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a Sentinel Hub collection's bands from the SDK (offline, no auth).

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (used to resolve a curated
            key to its `sh_collection` name).
        dataset: A curated key (e.g. `sentinel-2-l2a`) or an SDK collection
            name (e.g. `SENTINEL2_L2A`).

    Returns:
        Mapping of band name to `{units, output_types}`, plus — when
        `dataset` is a curated key — a `collection:<key>` row carrying the
        bound `sh_collection`, native `resolution`, and `cadence`.

    Raises:
        KeyError: If `dataset` resolves to no known `DataCollection`.
    """
    from earthlens.sentinel_hub._helpers import import_sentinelhub

    sentinelhub = import_sentinelhub()
    record = catalog.datasets.get(dataset)
    name = getattr(record, "sh_collection", None) or dataset
    collection = sentinelhub.DataCollection[name]
    schema: dict[str, dict[str, Any]] = {}
    if record is not None:
        schema[f"collection:{dataset}"] = {
            "sh_collection": name,
            "resolution": getattr(record, "resolution", None),
            "cadence": getattr(record, "cadence", None),
        }
    for band in getattr(collection, "bands", None) or []:
        units = getattr(band, "units", None) or ()
        types = getattr(band, "output_types", None) or ()
        schema[str(band.name)] = {
            "units": ", ".join(str(getattr(u, "value", u)) for u in units),
            "output_types": ", ".join(getattr(t, "__name__", str(t)) for t in types),
        }
    return schema


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each Sentinel Hub recipe's evalscript `.js` must be well-formed.

    Mirrors `tools/sentinel_hub/refresh_sh_catalog.py:validate-recipe`
    (offline): the bundled `.js` must exist, start with `//VERSION=3`, and
    a `"stats"` recipe must declare a `dataMask` band.

    Args:
        catalog: The loaded Sentinel Hub `Catalog` (exposes `recipes`).

    Returns:
        `(checked, issues)` — the recipe count and one message per problem.
    """
    from earthlens.sentinel_hub import read_evalscript

    recipes = getattr(catalog, "recipes", None) or {}
    issues: list[str] = []
    for key, recipe in recipes.items():
        script_name = getattr(recipe, "evalscript", None)
        if not script_name:
            continue
        try:
            script = read_evalscript(script_name)
        except FileNotFoundError as exc:
            issues.append(f"{key}: {exc}")
            continue
        if script.splitlines()[0].strip() != "//VERSION=3":
            issues.append(f"{key}: {script_name} does not start with //VERSION=3")
        if getattr(recipe, "kind", None) == "stats" and "dataMask" not in script:
            issues.append(f"{key}: {script_name} stats recipe has no dataMask band")
    return len(recipes), issues
