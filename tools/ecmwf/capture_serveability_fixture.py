"""Refresh the frozen constraints snapshot the serveability test judges against.

`test_no_shipped_row_is_unserveable_against_the_recorded_store` runs on every
PR and must be deterministic, so it feeds `audit_serveability` a recorded
snapshot of every ECMWF dataset's `constraints.json` rather than the live
stores. This operator script captures that snapshot.

Run it when the live stores legitimately change - a new data version, a new
partition dimension, a retired cadence - which the weekly
`..._against_the_live_store` (`e2e`) test and the scheduled catalog-drift
workflow are what surface. In the same commit, move the affected count in the
recorded-store test's `_KNOWN_UNSERVEABLE` so the two stay in step.

Usage:

    python tools/ecmwf/capture_serveability_fixture.py

It writes, relative to the repo, to:

    libs/providers/atmosphere/tests/test_ecmwf/fixtures/serveability_constraints.json.gz

The fetch is unauthenticated (constraints documents are public) and visits one
`constraints.json` per curated dataset. A dataset whose fetch fails is recorded
as an empty block list, which the offline audit treats as nothing to judge.
(The live audit instead flags an unreachable store as a distinct
`<constraints unreadable>` finding, but `_counts` in the test drops that
marker, so the two agree on the count either way.)
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

from earthlens.ecmwf._hydrate import _promises_data
from earthlens.ecmwf.catalog import Catalog
from earthlens.ecmwf.cli import _ecmwf_constraints

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "libs/providers/atmosphere/tests/test_ecmwf/fixtures"
    / "serveability_constraints.json.gz"
)


def capture(out: Path) -> None:
    """Fetch every curated dataset's constraints and write the gzipped snapshot.

    Args:
        out: The `.json.gz` path to write. Parent directories are created.
    """
    datasets = Catalog().datasets
    snapshot: dict[str, list[dict]] = {}
    for name, dataset in datasets.items():
        if not any(_promises_data(row) for row in dataset.variables.values()):
            continue
        try:
            blocks = _ecmwf_constraints(name) or []
        except Exception as exc:  # noqa: BLE001 - a failed fetch is recorded as []
            print(f"  !! {name}: fetch failed ({exc}); recording []", file=sys.stderr)
            blocks = []
        snapshot[name] = blocks
        print(f"  {len(blocks):5} blocks  {name}", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    # Canonicalise: sort each block's list values. The audit reads them as sets
    # (`variable` membership, offered-value comparisons), so order is never
    # semantic - sorting makes a non-semantic upstream reordering leave the
    # committed bytes unchanged, rather than churning the snapshot.
    canonical = {
        name: [
            {
                key: sorted(value) if isinstance(value, list) else value
                for key, value in block.items()
            }
            for block in blocks
        ]
        for name, blocks in snapshot.items()
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    # mtime=0 drops the gzip timestamp, so - given the same zlib build and the
    # canonicalisation above - an unchanged store yields identical bytes and git
    # shows nothing to commit. The compressed bytes are not guaranteed identical
    # across zlib versions, so a refresh on a different toolchain may still diff.
    with out.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
            fh.write(payload)
    print(
        f"\nwrote {len(snapshot)} dataset(s) to {out} ({out.stat().st_size} gz bytes)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    capture(_FIXTURE)
