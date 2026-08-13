"""Catalog-tooling handlers for the JAXA backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). JAXA spans three protocols —
`jaxa-earth` (STAC COG), `gportal` (numeric ids), and `ptree` — whose live
listings come from the `jaxa.earth` / `gportal` SDKs (imported lazily) and, for
P-Tree, from the bundled catalog itself.
"""

from __future__ import annotations

import re
from typing import Any

from earthlens.cli.toolkit import index_writer

#: Persist a live fetch back into the bundled `available_datasets` index.
writer = index_writer("available_datasets")


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every live JAXA dataset id from all three protocols.

    Walks the two SDKs' authoritative listings: `jaxa.earth`'s STAC
    catalog (118 COG collections at A1 capture time) for the
    `jaxa-earth` group, and `gportal.datasets()` (799 numeric dataset
    ids at A1 capture time) for the `gportal` group. The `ptree` group
    is derived **live** from the bundled JAXA catalog's `ptree` rows
    (their `short_name` field) — P-Tree has no discoverable listing
    endpoint, so the local catalog is the authoritative source; a
    curator adding a new `protocol: ptree` row therefore reaches
    `_index.yaml` automatically on the next `refresh --write`.

    Args:
        catalog: The loaded JAXA `Catalog`. Used to derive the `ptree`
            group; the `jaxa-earth` and `gportal` groups still come
            from the SDKs.

    Returns:
        Three-group mapping:
        `{"jaxa-earth": [STAC collection ids], "gportal": [numeric
        ids], "ptree": [product tokens]}`.
    """
    import gportal as _gportal  # type: ignore[import-not-found]

    from jaxa.earth import je as _je  # type: ignore[import-not-found]

    je_ids, _ = _je.ImageCollectionList().filter_name()
    gp_tree = _gportal.datasets()

    gp_ids: list[str] = []
    stack: list[Any] = [gp_tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            gp_ids.extend(str(x) for x in node)
    ptree_ids = sorted(
        {
            row.short_name
            for row in catalog.datasets.values()
            if row.protocol == "ptree" and row.short_name
        }
    )
    return {
        "jaxa-earth": sorted(set(str(c) for c in je_ids)),
        "gportal": sorted(set(gp_ids)),
        "ptree": ptree_ids,
    }


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe one JAXA row's live schema (band list for jaxa-earth, props for gportal).

    For a `jaxa-earth` row, look the collection up in
    `je.ImageCollectionList().filter_name()` (which returns
    `(ids, bands_per_id)` tuples — the SDK's catalog dump used by the
    refresh path) and return one entry per band. For a `gportal` row,
    run an anonymous `gportal.search` for a 1-day window and return the
    first product's flattened properties as the schema. Both paths are
    network-bound; the SDKs are imported lazily via the catalog row's
    branch module.

    Args:
        catalog: The loaded JAXA `Catalog` (resolves a key's protocol +
            upstream id).
        dataset: A curated key, a raw STAC collection name, or a raw
            G-Portal numeric id.

    Returns:
        Mapping of schema-entry name to `{...}` info dict.
    """
    row = catalog.get(dataset)
    if row.protocol == "jaxa-earth":
        from jaxa.earth import je  # type: ignore[import-not-found]

        ids, bands_per_id = je.ImageCollectionList().filter_name()
        try:
            idx = list(ids).index(row.collection)
        except ValueError:
            return {}
        return {b: {"role": "band"} for b in bands_per_id[idx]}
    import gportal  # type: ignore[import-not-found]

    result = gportal.search(dataset_ids=[row.short_name], count=1)
    products = list(result.products())
    if not products:
        return {}
    flat = products[0].flatten_properties()
    return {k: {"value": str(v)[:80]} for k, v in flat.items()}


def _walk_gportal(node: Any, mission: str = "", level: str = ""):
    """Yield `(mission, level, leaf_id)` triples from a gportal.datasets() tree.

    The tree's top level is `mission -> level -> sensor -> [ids]`; the
    middle layers vary. Anything that's a list of leaf strings is treated
    as the product-id list, parented by whatever level we last saw.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            next_mission = mission or key
            next_level = level if mission else ""
            if mission and not level:
                next_level = key
            yield from _walk_gportal(value, next_mission, next_level)
    elif isinstance(node, list):
        for item in node:
            yield (mission, level, str(item))


def emitter(catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed a JAXA `datasets:` row from a STAC name or G-Portal numeric id.

    The upstream id's shape decides the protocol: a `JAXA.*` / `NASA.*`
    / `Copernicus.*` string seeds a `jaxa-earth` row (with the default
    band looked up via `je.ImageCollectionList()`); a 7-9 digit numeric
    string seeds a `gportal` row (with the description built from
    `gportal.datasets()`'s mission / level / product path).

    Args:
        catalog: The loaded JAXA `Catalog` (unused; the SDKs are the
            authoritative sources).
        upstream_id: The STAC collection name or G-Portal numeric id.
        **opts: Unused.

    Returns:
        A row dict matching the bundled YAML's `datasets:` shape.
    """
    del catalog
    if re.match(r"^\d{7,9}$", upstream_id):
        # G-Portal numeric id — walk the live tree to find its mission / path.
        import gportal  # type: ignore[import-not-found]

        tree = gportal.datasets()
        for mission, level, path in _walk_gportal(tree):
            if path == upstream_id:
                return {
                    "protocol": "gportal",
                    "short_name": upstream_id,
                    "description": f"{mission} / {level}",
                }
        return {
            "protocol": "gportal",
            "short_name": upstream_id,
            "description": "(unrecognised G-Portal id; verify upstream)",
        }
    # jaxa-earth STAC collection.
    from jaxa.earth import je  # type: ignore[import-not-found]

    ids, bands_per_id = je.ImageCollectionList().filter_name()
    bands: list[str] = []
    try:
        idx = list(ids).index(upstream_id)
        bands = list(bands_per_id[idx])
    except ValueError:
        pass
    row: dict[str, Any] = {
        "protocol": "jaxa-earth",
        "collection": upstream_id,
    }
    if bands:
        row["default_band"] = bands[0]
    return row


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Validate each JAXA row's protocol-specific identifier.

    The `Dataset` pydantic model already enforces the cross-field
    invariant (`jaxa-earth` needs `collection`, `gportal` needs
    `short_name`), so a clean catalog load reaches this validator with
    every row well-formed. This lint adds two cheap cross-row checks the
    model can't see:

    * a `jaxa-earth` row without a `default_band` is flagged — the
      backend's branch raises a hard error at fetch time, so catching
      it offline is friendlier;
    * any curated `short_name` / `collection` that doesn't appear in
      the YAML's `available_datasets:` index is flagged — that index is
      rewritten by `earthlens datasets refresh jaxa --write`, so drift
      surfaces here without a network round-trip.
    """
    available = set(catalog.available_datasets or ())
    issues: list[str] = []
    for key, row in catalog.datasets.items():
        if row.protocol == "jaxa-earth":
            if not row.default_band:
                issues.append(
                    f"{key}: jaxa-earth row missing `default_band` (the branch "
                    "will reject fetches without an explicit bands= override)"
                )
            if available and row.collection not in available:
                issues.append(
                    f"{key}: collection {row.collection!r} not in the bundled "
                    "`available_datasets:` index — refresh may have drifted"
                )
        else:
            if available and row.short_name not in available:
                issues.append(
                    f"{key}: short_name {row.short_name!r} not in the bundled "
                    "`available_datasets:` index — refresh may have drifted"
                )
    return len(catalog.datasets), issues
