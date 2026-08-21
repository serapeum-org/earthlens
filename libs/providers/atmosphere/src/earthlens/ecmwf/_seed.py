"""Bulk-seed every uncurated Copernicus dataset into the catalog shards.

Powers `curate ecmwf --all --write`: for every dataset in the per-store
`available_datasets:` index that has no curated row yet, fetch its live
`form.json`, emit a loader-valid row, and splice it into the right per-family
shard (auto-categorised by :func:`earthlens.ecmwf._categories.categorise_dataset`).
A seed only — `nc_variable` / `units` are placeholders the `--fill-empty`
hydrate step fills from a live retrieve.

The live `form.json` fetch sits behind :func:`earthlens.cli.stanza.emit_stanza`,
so the driver itself performs no I/O.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.adapter import list_backends
from earthlens.cli.stanza import emit_stanza, write_stanza


def bulk_seed_uncurated(limit: int | None = None) -> dict[str, Any]:
    """Seed a curated row for every uncurated Copernicus dataset, in place.

    The uncurated set is `available_datasets - datasets`: every id in the
    per-store availability index that has no curated row yet (the synthesised
    `*-monthly-means` keys are already in `datasets`, so they are correctly
    excluded). Each id is seeded from its live `form.json` and spliced into
    its auto-categorised per-family shard. An id whose form cannot be fetched
    (a non-`ok` emit) is skipped and recorded; a duplicate key (an id already
    curated in its shard, e.g. on an idempotent re-run) is skipped silently.

    Args:
        limit: Only seed the first `limit` uncurated datasets (alphabetical).

    Returns:
        A summary `{candidates, seeded, skipped, failed}` mapping — the number
        of uncurated ids attempted, rows written, ids skipped, and the
        `(id, detail)` pairs whose live form fetch failed.
    """
    from earthlens.ecmwf import Catalog
    from earthlens.ecmwf.catalog import clear_catalog_cache

    catalog = Catalog()
    uncurated = sorted(set(catalog.available_datasets) - set(catalog.datasets))
    if limit:
        uncurated = uncurated[:limit]
    info = next(b for b in list_backends() if b.provider == "ecmwf")

    seeded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    for dataset_id in uncurated:
        result = emit_stanza(info, dataset_id)
        if result.status != "ok":
            skipped += 1
            failed.append((dataset_id, result.detail))
            continue
        try:
            write_stanza(info, result, None)
        except ValueError:
            # A key already curated in its shard (idempotent re-run) — skip it.
            skipped += 1
            continue
        seeded += 1

    clear_catalog_cache()
    return {
        "candidates": len(uncurated),
        "seeded": seeded,
        "skipped": skipped,
        "failed": failed,
    }
