"""Catalog-tooling handlers for the EUMETSAT backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). Listing / browsing collections
needs no credentials (only data *access* does), so every handler here is public.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from earthlens.cli.toolkit import (
    curated_collection_ids,
    get_json,
    index_writer,
)

_BROWSE_URL = "https://api.eumetsat.int/data/browse/collections"

#: Persists a live collection-id fetch into the bundled `available_datasets:` block.
writer = index_writer("available_datasets")

#: Curated-id resolver over the catalog's collection ids (the `audit` axis).
curated_ids = curated_collection_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List EUMETSAT collection ids from the public browse endpoint.

    Args:
        catalog: The loaded EUMETSAT `Catalog` (unused; the endpoint is fixed).

    Returns:
        A single-group mapping `{"eumetsat": [sorted collection ids]}`.
    """
    body = get_json(_BROWSE_URL, params={"format": "json"})
    ids = sorted(
        {str(link["title"]) for link in body.get("links", []) if link.get("title")}
    )
    return {"eumetsat": ids}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an EUMETSAT collection's public browse metadata (no auth).

    Args:
        catalog: The loaded EUMETSAT `Catalog` (resolves a key's `collection_id`).
        dataset: A curated key or an `EO:EUM:DAT:…` collection id.

    Returns:
        A single-entry mapping `{collection_id: {title, abstract, date, updated}}`.
    """
    record = catalog.datasets.get(dataset)
    collection_id = getattr(record, "collection_id", None) or dataset
    body = get_json(
        f"{_BROWSE_URL}/{quote(collection_id, safe='')}",
        params={"format": "json"},
    )
    props = (body.get("collection") or {}).get("properties") or {}
    return {
        collection_id: {
            "title": props.get("title"),
            "abstract": (props.get("abstract") or "")[:200],
            "date": props.get("date"),
            "updated": props.get("updated"),
        }
    }


def _detail(collection_id: str) -> dict[str, Any]:
    """Return one EUMETSAT collection's public browse metadata."""
    url = f"{_BROWSE_URL}/{quote(collection_id, safe='')}"
    return get_json(url, params={"format": "json"})


def emitter(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed an EUMETSAT `datasets:` row from the public browse metadata.

    Args:
        catalog: The loaded EUMETSAT `Catalog` (unused).
        upstream_id: The `EO:EUM:DAT:…` collection id.
        **opts: `group` (Data Store group label).

    Returns:
        The seeded row; the maintainer fills `format` / `selectors` /
        `tailor_product_type` after vetting.
    """
    if not opts.get("minimal"):
        _detail(upstream_id)  # fail loud if the id is unreachable
    return {
        "collection_id": upstream_id,
        "group": str(opts.get("group") or "MSG"),
        "output_kind": "raster",
        "format": "",
        "selectors": [],
        "tailor_product_type": None,
    }
