"""Catalog-tooling handlers for the Google Earth Engine backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._imagery_cli`). The refresher / prober / emitter
/ coverage classifier all read the **public** Earth Engine STAC catalog (no
credentials); the live-band hydrate fallback and the `--fill-empty` bulk pass are
credentialed (`GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY`).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from earthlens.base import safe_filename
from earthlens.cli.toolkit import (
    COVERAGE_BUCKETS,
    bands_from_summaries,
    get_json,
    index_writer,
)
from earthlens.gee import _hydrate
from earthlens.gee._categories import categorise_asset  # noqa: F401 — role target

#: Public Earth Engine STAC catalog root (no credentials for the catalog).
_GEE_STAC_ROOT = "https://storage.googleapis.com/earthengine-stac/catalog/catalog.json"

#: Public per-asset STAC-doc URL base (the id -> doc filename convention).
_GEE_STAC_DOC_BASE = "https://storage.googleapis.com/earthengine-stac/catalog"

#: The whole-globe bbox a per-asset extent is dropped as redundant against.
_GEE_GLOBAL_BBOX = [-180.0, -90.0, 180.0, 90.0]

#: STAC `eo:bands` metadata keys the emitter / classifier read repeatedly.
_GEE_UNITS_KEY = "gee:units"
_GEE_SCALE_KEY = "gee:scale"

#: Persist a live fetch back into the bundled `available_datasets` index.
writer = index_writer("available_datasets")


def _gee_dataset_hrefs() -> list[str]:
    """Walk the public EE STAC tree and return every dataset STAC-doc href.

    BFS over `rel="child"` links from the root (absolute hrefs); links to
    `…/catalog.json` are sub-catalogs to recurse, the rest are dataset docs.

    Returns:
        The dataset STAC-document hrefs (one per Earth Engine asset).
    """
    hrefs: list[str] = []
    queue = [_GEE_STAC_ROOT]
    seen: set[str] = set()
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        try:
            node = get_json(url)
        except Exception:  # noqa: BLE001 — skip an unreachable sub-catalog  # nosec B112
            continue
        for link in node.get("links", []):
            if link.get("rel") != "child":
                continue
            href = link.get("href")
            if not href:
                continue
            (queue if href.endswith("/catalog.json") else hrefs).append(href)
    return hrefs


def _gee_fetch_id(href: str) -> str | None:
    """Return a dataset STAC doc's `id` (its EE asset id), or None on error."""
    try:
        return get_json(href).get("id")
    except Exception:  # noqa: BLE001 — a single unreachable doc is skipped
        return None


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List every Earth Engine asset id from the public STAC catalog.

    Walks the STAC tree for dataset docs, then fetches each doc's `id`
    concurrently (pure HTTP, no SDK / credentials).

    Args:
        _catalog: The loaded GEE `Catalog` (unused; the STAC tree is the source).

    Returns:
        A single-group mapping `{"gee": [sorted asset ids]}`.
    """
    hrefs = _gee_dataset_hrefs()
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = {str(cid) for cid in pool.map(_gee_fetch_id, hrefs) if cid}
    return {"gee": sorted(ids)}


def prober(_catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a GEE asset's band schema from its public EE STAC document.

    Args:
        _catalog: The loaded GEE `Catalog` (unused; the STAC doc is the source).
        dataset: The Earth Engine asset id (e.g. `NASA/GDDP-CMIP6`).

    Returns:
        Mapping of band name to `{units, gsd, description}`.
    """
    provider = dataset.split("/", 1)[0]
    filename = safe_filename(dataset) + ".json"
    url = f"{_GEE_STAC_DOC_BASE}/{provider}/{filename}"
    body = get_json(url)
    schema: dict[str, dict[str, Any]] = {}
    for band in bands_from_summaries(body):
        name = band.get("name")
        if not name:
            continue
        gsd = band.get("gsd")
        schema[str(name)] = {
            "units": band.get(_GEE_UNITS_KEY),
            "gsd": gsd[0] if isinstance(gsd, list) and gsd else gsd,
            "description": (band.get("description") or "").strip()[:60],
        }
    return schema


def _gee_stac_doc(asset_id: str) -> dict[str, Any]:
    """Return an Earth Engine asset's public STAC document."""
    provider = asset_id.split("/", 1)[0]
    filename = safe_filename(asset_id) + ".json"
    return get_json(f"{_GEE_STAC_DOC_BASE}/{provider}/{filename}")


def _gee_live_bands(asset_id: str) -> tuple[str, dict[str, dict[str, Any]]]:
    """Query Earth Engine live for an asset's `(ee_type, bands)` (needs creds).

    The credentialed fallback for assets the public STAC tree can't reach
    (mainly community `projects/...` ids). Authenticates the service
    account (`GEE_SERVICE_ACCOUNT` / `GEE_SERVICE_KEY`) and reads the band
    names off the asset's first image.

    Args:
        asset_id: The Earth Engine asset id.

    Returns:
        `(ee_type, {band: {}})` — the asset type (lowercased) and its bands.
    """
    import ee

    from earthlens.gee.auth import EarthEngineAuth

    EarthEngineAuth.initialize(
        os.environ.get("GEE_SERVICE_ACCOUNT", ""),
        os.environ.get("GEE_SERVICE_KEY", ""),
        os.environ.get("GEE_PROJECT"),
    )
    ee_type = (ee.data.getAsset(asset_id).get("type") or "IMAGE_COLLECTION").lower()
    image = (
        ee.Image(asset_id)
        if ee_type == "image"
        else ee.ImageCollection(asset_id).first()
    )
    bands = ee.Image(image).bandNames().getInfo() or []
    return ee_type, {str(band): {} for band in bands}


def _gsd_to_metres(gsd: Any) -> float | None:
    """Return a band `gsd` as a float (unwrapping a `[value]` list)."""
    value = gsd[0] if isinstance(gsd, list) and gsd else gsd
    return float(value) if isinstance(value, (int, float)) else None


def emitter(_catalog: Any, upstream_id: str, **opts: Any) -> dict[str, Any]:
    """Seed a GEE `datasets:` row from the asset's public STAC document.

    Args:
        _catalog: The loaded GEE `Catalog` (unused; STAC is the source).
        upstream_id: The Earth Engine asset id (e.g. `NASA/GDDP-CMIP6`).
        **opts: `minimal` emits a placeholder row with empty bands;
            `hydrate` reads the bands live from Earth Engine (needs creds —
            the fallback for assets the public STAC tree can't reach).

    Returns:
        The seeded row (title / ee_type / cadence / extent / bands).
    """
    if opts.get("minimal"):
        return {
            "title": upstream_id,
            "ee_type": "image_collection",
            "default_reducer": "median",
            "bands": {},
        }
    if opts.get("hydrate"):
        ee_type, live_bands = _gee_live_bands(upstream_id)
        return {
            "title": upstream_id,
            "ee_type": ee_type,
            "default_reducer": "median",
            "bands": live_bands,
        }
    doc = _gee_stac_doc(upstream_id)
    extent = doc.get("extent", {})
    temporal = (extent.get("temporal", {}).get("interval") or [[None, None]])[0]
    spatial_bbox = (extent.get("spatial", {}).get("bbox") or [None])[0]
    bands = doc.get("summaries", {}).get("eo:bands") or []
    resolutions = [r for r in (_gsd_to_metres(b.get("gsd")) for b in bands) if r]
    providers = doc.get("providers") or []
    interval = doc.get("gee:interval") or {}

    row: dict[str, Any] = {
        "title": (doc.get("title") or upstream_id).splitlines()[0],
        "ee_type": doc.get("gee:type", "image_collection"),
    }
    if providers:
        row["provider"] = providers[0].get("name", "") or ""
    if interval.get("interval") and interval.get("unit"):
        row["cadence"] = {"interval": interval["interval"], "unit": interval["unit"]}
    if resolutions:
        row["spatial_resolution"] = min(resolutions)
    start = (temporal[0] or "")[:10] if temporal and temporal[0] else "1970-01-01"
    row["extent"] = {"start_date": start, "end_date": None}
    if spatial_bbox and list(spatial_bbox) != _GEE_GLOBAL_BBOX:
        row["extent"]["bbox"] = list(spatial_bbox)
    row["default_reducer"] = "median"
    row["bands"] = {
        band["name"]: {
            "description": (
                band.get("description") or band.get("name", "")
            ).splitlines()[0],
            **({"units": band[_GEE_UNITS_KEY]} if band.get(_GEE_UNITS_KEY) else {}),
            **(
                {"scale": band[_GEE_SCALE_KEY]}
                if band.get(_GEE_SCALE_KEY) is not None
                else {}
            ),
        }
        for band in bands
        if band.get("name")
    }
    return row


def _gee_stac_or_none(asset_id: str) -> dict[str, Any] | None:
    """Fetch one Earth Engine asset's public STAC document, or None on error.

    Args:
        asset_id: The Earth Engine asset id (e.g. `LANDSAT/LC09/C02/T1_L2`).

    Returns:
        The parsed STAC document, or None when it 404s / is unreadable.
    """
    provider = asset_id.split("/", 1)[0]
    url = f"{_GEE_STAC_DOC_BASE}/{provider}/{safe_filename(asset_id)}.json"
    try:
        return get_json(url)
    except Exception:  # noqa: BLE001 — a missing/unreadable doc -> "missing"
        return None


def _gee_classify(asset_id: str, curated: set[str]) -> str:
    """Bucket one asset id for the curation-coverage report.

    Args:
        asset_id: The Earth Engine asset id to classify.
        curated: The set of asset ids already in the curated `datasets:` map.

    Returns:
        One of `"DONE"` (already curated), `"table"` (a FeatureCollection,
        out of raster scope), `"addressable"` (has bands carrying usable
        metadata — a `gee:units` / `gee:scale`), `"thin"` (no usable band
        metadata, needs hand-modelling), or `"missing"` (no STAC doc).
    """
    if asset_id in curated:
        return "DONE"
    doc = _gee_stac_or_none(asset_id)
    if doc is None:
        return "missing"
    if doc.get("gee:type") == "table":
        return "table"
    bands = (doc.get("summaries", {}) or {}).get("eo:bands") or []
    has_meta = any(
        b.get(_GEE_UNITS_KEY) or b.get(_GEE_SCALE_KEY) is not None for b in bands
    )
    return "addressable" if (bands and has_meta) else "thin"


def coverage(catalog: Any) -> tuple[dict[str, int], list[str]]:
    """Classify every `available_datasets:` id of the GEE catalog.

    Args:
        catalog: The loaded GEE `Catalog`.

    Returns:
        `(counts, todo)` — per-bucket counts and the sorted `addressable`
        ids not yet curated.

    Raises:
        ValueError: If the `available_datasets:` index is empty.
    """
    available = [str(ident) for ident in getattr(catalog, "available_datasets", [])]
    if not available:
        raise ValueError(
            "available_datasets: is empty — run `refresh gee --write` first"
        )
    curated = set(catalog.datasets)
    buckets: dict[str, list[str]] = {}
    for asset_id in available:
        buckets.setdefault(_gee_classify(asset_id, curated), []).append(asset_id)
    counts = {bucket: len(buckets.get(bucket, [])) for bucket in COVERAGE_BUCKETS}
    return counts, sorted(buckets.get("addressable", []))


def hydrator(*, limit: int | None = None, timeout: int | None = None) -> dict[str, Any]:
    """Bulk-hydrate placeholder GEE rows in place (`curate gee --fill-empty`).

    Args:
        limit: Only hydrate the first N placeholder rows (None = all).
        timeout: Unused for GEE (the Earth Engine reads have no per-row
            deadline); accepted for a uniform hydrator signature.

    Returns:
        The `{candidates, hydrated, skipped}` summary from the live pass.
    """
    del timeout
    return _hydrate.bulk_hydrate_empty(limit=limit)
