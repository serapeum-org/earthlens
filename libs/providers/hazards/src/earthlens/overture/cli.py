"""Catalog-tooling handlers for the Overture backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`) and dispatched by
`earthlens datasets refresh` / `audit` / `probe` / `validate`. Overture's
refreshable axis is its date-stamped *releases*, so its refresher diffs against
the catalog's `available_releases:` index rather than `available_datasets:`.

The shared machinery (`index_writer`, `require`, `lint`, `BackendInfo`) comes
from the public `earthlens.cli.toolkit`; the live reads use the public
`overturemaps` SDK, imported lazily so discovery never pulls it in.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import index_writer, lint, require

#: A tiny bbox (Times Square block: W, S, E, N) for the Overture live reads.
_OVERTURE_BBOX = (-73.9876, 40.7561, -73.9851, 40.7577)

#: Persists a live release fetch into the bundled `available_releases:` block.
writer = index_writer("available_releases")


def _release_ids() -> list[str]:
    """Return every available Overture release id (`overturemaps` SDK).

    `get_available_releases()` returns a `(all_releases, latest)` tuple; only
    the release list is taken.

    Returns:
        Every release id the SDK reports, as strings.
    """
    from overturemaps.core import get_available_releases

    result = get_available_releases()
    releases = result[0] if isinstance(result, tuple) else result
    return [str(release) for release in releases]


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List every available Overture release via the `overturemaps` SDK.

    Args:
        _catalog: The loaded Overture `Catalog` (unused; the SDK is the source).

    Returns:
        A single-group mapping `{"overture": [sorted release ids]}`.
    """
    return {"overture": sorted(set(_release_ids()))}


def curated_ids(catalog: Any) -> list[str]:
    """Return the Overture releases the catalog tracks (its refresh axis).

    Args:
        catalog: The loaded Overture `Catalog`.

    Returns:
        The sorted release ids in the catalog's `available_releases`.
    """
    return sorted(
        str(release) for release in getattr(catalog, "available_releases", [])
    )


def _columns(overture_type: str) -> dict[str, str]:
    """Return `{column: dtype}` for a tiny Overture bbox fetch (public SDK)."""
    from overturemaps import core

    frame = core.geodataframe(overture_type, bbox=_OVERTURE_BBOX)
    return {str(name): str(dtype) for name, dtype in frame.dtypes.items()}


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe an Overture feature type's column schema (public `overturemaps`).

    Fetches a tiny bbox for the type and records each column's dtype.

    Args:
        catalog: The loaded Overture `Catalog` (resolves a theme key's
            `default_type`).
        dataset: A curated theme key or an Overture feature type.

    Returns:
        Mapping of column name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    overture_type = getattr(record, "default_type", None) or dataset
    return {
        column: {"dtype": dtype} for column, dtype in _columns(overture_type).items()
    }


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each Overture theme needs types and a default_type drawn from them.

    Args:
        catalog: The loaded Overture `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """

    def check(key: str, record: Any) -> list[str]:
        """Flag a theme missing types/default_type or whose default is unlisted."""
        issues = require(key, record, ("types", "default_type"))
        types = getattr(record, "types", None) or []
        default = getattr(record, "default_type", None)
        if default and types and default not in types:
            issues.append(f"{key}: default_type {default!r} not in types")
        return issues

    return lint(catalog, check)


def _live_sample(overture_type: str) -> tuple[int, bool]:
    """Fetch a tiny bbox; return `(row_count, has_sources_column)`."""
    from overturemaps.core import geodataframe

    frame = geodataframe(overture_type, bbox=_OVERTURE_BBOX)
    return len(frame), "sources" in frame.columns


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm each Overture type resolves live and carries a `sources` column.

    Args:
        catalog: The loaded Overture `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any live-reachability
        problems.
    """
    issues: list[str] = []
    for key, record in catalog.datasets.items():
        overture_type = getattr(record, "default_type", None) or key
        try:
            _rows, has_sources = _live_sample(overture_type)
        except Exception as exc:  # noqa: BLE001 — reported as drift
            issues.append(f"{key}/{overture_type}: fetch failed ({exc})")
            continue
        if not has_sources:
            issues.append(f"{key}/{overture_type}: no 'sources' column")
    return len(catalog.datasets), issues


__all__ = [
    "curated_ids",
    "live_validator",
    "prober",
    "refresher",
    "validator",
    "writer",
]
