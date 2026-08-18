"""Catalog-tooling handlers for the Overture backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`) and dispatched by
`earthlens datasets refresh` / `audit` / `probe` / `validate`. Overture's
refreshable axis is its date-stamped *releases*, so its refresher diffs against
the catalog's `available_releases:` index rather than `available_datasets:`.

The shared machinery (`index_writer`, `require`, `lint`, `BackendInfo`) comes
from the public `earthlens.cli.toolkit`; the live reads use the public
`overturemaps` SDK and `earthlens.overture.releases`, imported lazily so
discovery never pulls them in.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from earthlens.cli.toolkit import index_writer, lint, require

#: A tiny bbox (Times Square block: W, S, E, N) for the Overture live reads.
_OVERTURE_BBOX = (-73.9876, 40.7561, -73.9851, 40.7577)

#: Persists a live release fetch into the bundled `available_releases:` block.
writer = index_writer("available_releases")


def _release_ids() -> list[str]:
    """Return every available Overture release id.

    `get_available_releases()` returns an `(all_releases, latest)` tuple.
    Both halves are used and both are filtered through `is_release_id`,
    because only one of them is trustworthy: the SDK derives
    `all_releases` by splitting each STAC child href on `/` after
    stripping `./`, which yields `"https:"` now that the catalog serves
    absolute hrefs, while `latest` is read from a dedicated catalog field
    and is unaffected. Persisting `https:` as a release id is worse than
    persisting nothing, so anything that is not release-shaped is dropped.

    Dropping them silently would leave the index recording one of the two
    releases upstream publishes and report that as clean, so whenever the
    SDK's list looks lossy the ids are re-read from the catalog's child
    links (`earthlens.overture.releases.child_release_ids`) and the
    recovery is logged. A recovery that cannot reach the catalog is
    logged and skipped rather than failing the refresh — whatever the SDK
    did parse is still worth reporting.

    Returns:
        The available release ids, de-duplicated and sorted.

    Raises:
        ReleaseLookupError: If nothing upstream said parses as a release
            id. Refusing here keeps `--write` from blanking the bundled
            index, which is the backend's only offline fallback.
    """
    from overturemaps.core import get_available_releases

    from earthlens.overture.releases import (
        ReleaseLookupError,
        child_release_ids,
        is_release_id,
    )

    result = get_available_releases()
    if isinstance(result, tuple):
        listed, latest = (list(result) + [None, None])[:2]
    else:
        listed, latest = result, None
    reported = [str(release) for release in listed or []]
    if latest is not None:
        reported.append(str(latest))
    parsed = {ident for ident in reported if is_release_id(ident)}
    unparsed = [ident for ident in reported if not is_release_id(ident)]
    # Recover whenever the SDK's own list is unusable — every id mangled, or
    # no list at all. Either way `latest` alone would leave the index holding
    # one of the several releases upstream publishes and call that clean.
    if unparsed or not listed:
        try:
            recovered = {i for i in child_release_ids() if is_release_id(i)}
        except ReleaseLookupError as exc:
            logger.warning(
                f"The overturemaps SDK reported {len(unparsed)} unparsed "
                f"release id(s); could not recover them from the STAC "
                f"catalog either ({exc})."
            )
        else:
            logger.warning(
                f"The overturemaps SDK reported {len(unparsed)} unparsed "
                f"release id(s) ({sorted(set(unparsed))}); recovered "
                f"{len(recovered - parsed)} from the STAC catalog's child "
                "links."
            )
            parsed |= recovered
    if not parsed:
        raise ReleaseLookupError(
            "No Overture release id could be parsed from the STAC catalog "
            f"(the SDK reported {sorted(reported)}). Refusing to rewrite "
            "available_releases: with an empty list — it is the backend's "
            "offline fallback."
        )
    return sorted(parsed)


def refresher(_catalog: Any) -> dict[str, list[str]]:
    """List every available Overture release.

    Reads the `overturemaps` SDK, falling back to Overture's STAC catalog
    directly for the ids the SDK cannot parse (see `_release_ids`).

    Args:
        _catalog: The loaded Overture `Catalog` (unused; upstream is the
            source).

    Returns:
        A single-group mapping `{"overture": [sorted release ids]}`.
    """
    return {"overture": _release_ids()}


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
