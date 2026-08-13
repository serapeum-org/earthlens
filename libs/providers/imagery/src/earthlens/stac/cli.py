"""Catalog-tooling handlers for the STAC backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). Every read is public: the
refresher lists each endpoint's `/collections`, the prober samples one item's
asset schema, and `--write` rewrites the bundled `available_collections:` block.
"""

from __future__ import annotations

import importlib
from typing import Any

import yaml

from earthlens.cli.toolkit import (
    BackendInfo,
    curated_collection_ids,
    get_json,
)

#: Cap on `/collections` pages followed via `rel="next"` — a guard against a
#: misbehaving endpoint paginating forever.
_MAX_PAGES = 50

#: Curated-id resolver over the catalog's collection ids (the `audit` axis).
curated_ids = curated_collection_ids


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List collection ids per STAC endpoint, live.

    Args:
        catalog: The loaded STAC `Catalog` (exposes `endpoints`).

    Returns:
        A mapping of endpoint name to its sorted, de-duplicated collection ids.
    """
    grouped: dict[str, list[str]] = {}
    for name, endpoint in catalog.endpoints.items():
        ids: set[str] = set()
        url: str | None = endpoint.url.rstrip("/") + "/collections"
        pages = 0
        while url and pages < _MAX_PAGES:
            body = get_json(url)
            for collection in body.get("collections", []):
                cid = collection.get("id")
                if cid:
                    ids.add(str(cid))
            url = next(
                (
                    link.get("href")
                    for link in body.get("links", [])
                    if link.get("rel") == "next"
                ),
                None,
            )
            pages += 1
        grouped[name] = sorted(ids)
    return grouped


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Rewrite STAC's `available_collections:` block from a live fetch.

    Args:
        info: The STAC backend.
        grouped: Endpoint-name -> live collection ids (see `refresher`).

    Returns:
        The path of the file rewritten.

    Raises:
        ValueError: If the index file has no `available_collections:` block.
    """
    module = importlib.import_module(f"{info.module}.catalog")
    index_path = module.CATALOG_PATH / "_index.yaml"
    text = index_path.read_text(encoding="utf-8")
    marker = "\navailable_collections:"
    if marker not in text:
        raise ValueError(f"no available_collections block in {index_path}")
    head = text.split(marker, 1)[0].rstrip("\n")
    block = yaml.safe_dump(
        {"available_collections": grouped},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10000,
    )
    index_path.write_text(f"{head}\n\n{block}", encoding="utf-8")
    return str(index_path)


def _asset_fields(asset: Any) -> dict[str, Any]:
    """Return a plain field dict for a raw STAC asset (or a pystac `Asset`)."""
    if isinstance(asset, dict):
        return asset
    fields: dict[str, Any] = dict(getattr(asset, "extra_fields", {}) or {})
    media_type = getattr(asset, "media_type", None)
    if media_type is not None:
        fields.setdefault("type", media_type)
    return fields


def _asset_schema(item: Any) -> dict[str, dict[str, Any]]:
    """Extract a per-asset `{media_type, common_name, dtype, nodata}` schema.

    Reads each asset's media type and the first `raster:bands` / `eo:bands`
    entry — the fields the catalog `Asset` model curates.

    Args:
        item: A raw STAC item dict (or a pystac `Item`) with an `assets` map.

    Returns:
        Mapping of asset key to its schema (fields `None` when absent).
    """
    assets = getattr(item, "assets", None)
    if assets is None and isinstance(item, dict):
        assets = item.get("assets", {})
    schema: dict[str, dict[str, Any]] = {}
    for key, asset in (assets or {}).items():
        fields = _asset_fields(asset)
        first_raster = (fields.get("raster:bands") or [{}])[0]
        first_eo = (fields.get("eo:bands") or [{}])[0]
        schema[key] = {
            "media_type": fields.get("type"),
            "common_name": first_eo.get("common_name"),
            "dtype": first_raster.get("data_type"),
            "nodata": first_raster.get("nodata"),
        }
    return schema


def _endpoint_candidates(catalog: Any, dataset: str) -> list[tuple[Any, str]]:
    """Resolve `(endpoint, collection_id)` pairs to try for `dataset`."""
    record = catalog.datasets.get(dataset)
    endpoint_name = getattr(record, "endpoint", None)
    collection_id = getattr(record, "collection_id", None)
    if endpoint_name in getattr(catalog, "endpoints", {}) and collection_id:
        return [(catalog.endpoints[endpoint_name], collection_id)]
    return [(endpoint, dataset) for endpoint in catalog.endpoints.values()]


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Fetch one sample item for a STAC collection and extract its schema.

    Tries each candidate endpoint's `/collections/{id}/items?limit=1` until one
    yields an item.

    Args:
        catalog: The loaded STAC `Catalog`.
        dataset: A collection id (or curated catalog key).

    Returns:
        The per-asset schema from the sample item.

    Raises:
        ValueError: If no endpoint yields a sample item for `dataset`.
    """
    last_error: Exception | None = None
    for endpoint, collection_id in _endpoint_candidates(catalog, dataset):
        url = endpoint.url.rstrip("/") + f"/collections/{collection_id}/items?limit=1"
        try:
            body = get_json(url)
        except Exception as exc:  # noqa: BLE001 — try the next endpoint
            last_error = exc
            continue
        features = body.get("features") or []
        if features:
            return _asset_schema(features[0])
    suffix = f" (last error: {last_error})" if last_error else ""
    raise ValueError(f"no sample item found for {dataset!r}{suffix}")
