"""Catalog-tooling handlers for the openEO backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). Listing collections /
processes on the public CDSE openEO endpoint needs no credentials.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import (
    BackendInfo,
    bands_from_summaries,
    curated_collection_ids,
    flatten,
    get_json,
    index_path,
    replace_index_block,
)

_COLLECTIONS_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/collections"
_PROCESSES_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/processes"

#: Curated-id resolver over the catalog's collection ids (the `audit` axis).
curated_ids = curated_collection_ids


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List the CDSE openEO collection ids, live (public, anonymous).

    Args:
        _catalog: The loaded openEO `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"openeo": [sorted collection ids]}`.
    """
    body = get_json(_COLLECTIONS_URL)
    ids = sorted({str(c["id"]) for c in body.get("collections", []) if c.get("id")})
    return {"openeo": ids}


def _process_ids() -> list[str]:
    """List the live CDSE openEO process ids (public, anonymous)."""
    body = get_json(_PROCESSES_URL)
    return sorted({str(p["id"]) for p in body.get("processes", []) if p.get("id")})


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite openEO's `available_collections` AND `available_processes`.

    The collection index comes from the live fetch (`grouped`); the process
    index is fetched separately — so `--write` keeps both informational blocks
    of `_index.yaml` current.

    Args:
        info: The openEO backend.
        grouped: Group name -> live collection ids (see `refresher`).

    Returns:
        The path of the rewritten `_index.yaml`.
    """
    path = index_path(info)
    replace_index_block(path, "available_collections", flatten(grouped))
    replace_index_block(path, "available_processes", _process_ids())
    return str(path)


def prober(_catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an openEO collection's band schema (public `/collections/{id}`).

    Args:
        _catalog: The loaded openEO `Catalog` (unused; the endpoint is fixed).
        dataset: The collection id.

    Returns:
        Mapping of band name to `{common_name, dtype, gsd, unit}` (falling back
        to the `cube:dimensions` band names when the collection carries no
        `eo:bands`), plus one `dim:<axis>` row per non-band cube axis.
    """
    url = f"{_COLLECTIONS_URL}/{dataset}"
    body = get_json(url)
    schema: dict[str, dict[str, Any]] = {}
    bands = bands_from_summaries(body)
    dimensions = body.get("cube:dimensions", {}) or {}
    if bands:
        for band in bands:
            if band.get("name"):
                schema[str(band["name"])] = {
                    "common_name": band.get("common_name"),
                    "dtype": band.get("data_type"),
                    "gsd": band.get("gsd"),
                    "unit": band.get("unit"),
                }
    else:
        band_names: list[Any] = next(
            (
                dim.get("values", [])
                for dim in dimensions.values()
                if dim.get("type") == "bands"
            ),
            [],
        )
        schema = {str(name): {} for name in band_names}
    for name, dim in dimensions.items():
        if dim.get("type") == "bands":
            continue
        schema[f"dim:{name}"] = {
            "type": dim.get("type") or dim.get("axis"),
            "extent": dim.get("extent"),
            "step": dim.get("step"),
        }
    return schema


def _live_lists() -> tuple[set[str], set[str]]:
    """Return the live `(collection_ids, process_ids)` sets (public CDSE)."""
    collections = {
        c["id"]
        for c in get_json(_COLLECTIONS_URL).get("collections", [])
        if c.get("id")
    }
    processes = {
        p["id"] for p in get_json(_PROCESSES_URL).get("processes", []) if p.get("id")
    }
    return collections, processes


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each openEO recipe's base collection + processes exist live.

    Args:
        catalog: The loaded openEO `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    recipes = getattr(catalog, "recipes", None) or {}
    if not recipes:
        return 0, []
    collections, processes = _live_lists()
    issues: list[str] = []
    for key, recipe in recipes.items():
        base = getattr(recipe, "base_collection", None)
        if base and base not in collections:
            issues.append(f"{key}: base_collection {base!r} not served live")
        for process in getattr(recipe, "processes", None) or []:
            if process not in processes:
                issues.append(f"{key}: process {process!r} not served live")
    return len(recipes), issues
