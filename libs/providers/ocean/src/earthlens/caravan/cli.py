"""Catalog-tooling handlers for the Caravan backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._ocean_cli`). `refresher` reports release
drift against Zenodo (it never rewrites the catalog, so there is no writer — a
pin bump is a human decision); `validator` lints each extension's pinned,
self-consistent release shape.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from earthlens.cli.toolkit import get_json, lint

#: Zenodo's version-chain endpoint for one record.
_ZENODO_VERSIONS = "https://zenodo.org/api/records/{record}/versions"

#: Zenodo's search endpoint, used to discover Caravan extensions the catalog
#: does not yet know about.
_ZENODO_SEARCH = "https://zenodo.org/api/records"

#: Queries whose **union** surfaces every known Caravan extension record.
_DISCOVERY_QUERIES = (
    '"Caravan extension"',
    "Caravan AND CAMELS",
    "caravan AND keywords:Hydrology",
)

#: Terms that mark a hit as a plausible Caravan extension rather than something
#: else called a caravan (camel-trade archaeology, animal taxonomy).
_HYDROLOGY_TERMS = (
    "hydrolog",
    "streamflow",
    "runoff",
    "catchment",
    "discharge",
    "caravan extension",
    "caravan_extension",
    "caravan-",
)


def _is_hydrological(hit: dict[str, Any]) -> bool:
    """Whether a Zenodo hit looks like hydrology rather than a camel caravan.

    Args:
        hit: One record from a Zenodo search response.

    Returns:
        `True` when the title or keywords carry a hydrology term.
    """
    metadata = hit.get("metadata") or {}
    haystack = " ".join(
        [
            str(metadata.get("title", "")),
            *(str(k) for k in metadata.get("keywords") or []),
        ]
    ).casefold()
    return any(term in haystack for term in _HYDROLOGY_TERMS)


def _newer_releases(extension: Any, pinned: set[str]) -> list[str]:
    """List an extension's releases published after its newest pin.

    Args:
        extension: One catalog `Extension`.
        pinned: Every record id the catalog pins, across all extensions.

    Returns:
        `"<record> (<date>)"` for each newer release, sorted.
    """
    latest_pin = max(
        (version.release_date for version in extension.versions.values()), default=""
    )
    records = {
        archive.record
        for version in extension.versions.values()
        for archive in version.files.values()
    }
    newer: set[str] = set()
    for record in sorted(records):
        payload = get_json(_ZENODO_VERSIONS.format(record=record))
        for hit in (payload.get("hits") or {}).get("hits") or []:
            published = str((hit.get("metadata") or {}).get("publication_date", ""))
            if published > latest_pin and str(hit.get("id")) not in pinned:
                newer.add(f"{hit.get('id')} ({published})")
    return sorted(newer)


def _discovered(
    pinned: set[str], concepts: set[str], unsupported: set[str]
) -> list[str]:
    """Find Caravan records on Zenodo the catalog does not track at all.

    Args:
        pinned: Record ids the catalog pins.
        concepts: Concept ids the catalog already tracks.
        unsupported: Record ids deliberately not wrapped.

    Returns:
        `"<record> (<title>)"` for each untracked record, sorted.
    """
    discovered: set[str] = set()
    filtered: set[str] = set()
    for query in _DISCOVERY_QUERIES:
        payload = get_json(
            _ZENODO_SEARCH, params={"q": query, "size": 25, "sort": "newest"}
        )
        for hit in (payload.get("hits") or {}).get("hits") or []:
            record = str(hit.get("id"))
            if (
                record in pinned
                or str(hit.get("conceptrecid")) in concepts
                or record in unsupported
            ):
                continue
            if not _is_hydrological(hit):
                # Never drop a search hit silently: a false negative here is
                # exactly how an extension stays invisible.
                filtered.add(record)
                continue
            title = str((hit.get("metadata") or {}).get("title", ""))[:60]
            discovered.add(f"{record} ({title})")
    if filtered:
        logger.debug(
            f"caravan: {len(filtered)} record(s) filtered as non-hydrological "
            f"({', '.join(sorted(filtered))})"
        )
    return sorted(discovered)


def refresher(catalog: Any) -> dict[str, list[str]]:
    """Report Caravan release drift: newer versions, and undiscovered extensions.

    A pinned extension can gain a newer release, and a brand-new community
    extension is published as its own record (never in a pinned version chain);
    neither is visible from the curated rows alone. This reports; it never
    rewrites the catalog — hence no writer, so `--write` says "live read only".

    Args:
        catalog: The loaded Caravan `Catalog`.

    Returns:
        One group per extension holding only the releases published *after* its
        pin (empty when current), plus a `discovered` group naming Caravan
        records whose concept the catalog does not track at all.
    """
    pinned = {
        str(archive.record)
        for extension in catalog.datasets.values()
        for version in extension.versions.values()
        for archive in version.files.values()
    }
    concepts = {
        str(doi).rsplit(".", 1)[-1]
        for extension in catalog.datasets.values()
        for doi in (
            extension.concept_doi,
            getattr(extension, "concept_doi_csv", ""),
        )
        if doi
    }
    unsupported = {
        str(record)
        for entry in getattr(catalog, "extension_index", []) or []
        if isinstance(entry, dict) and not entry.get("supported", True)
        for record in (entry.get("records") or [])
    }
    grouped = {
        key: _newer_releases(extension, pinned)
        for key, extension in sorted(catalog.datasets.items())
    }
    grouped["discovered"] = _discovered(pinned, concepts, unsupported)
    return grouped


def _archive_issues(label: str, archive: Any) -> list[str]:
    """Flag one archive file that could not be fetched or verified."""
    issues = []
    if not archive.record:
        issues.append(f"{label}: no pinned Zenodo record")
    if not archive.md5:
        issues.append(f"{label}: no md5 to verify the download")
    if archive.size <= 0:
        issues.append(f"{label}: size must be positive")
    if archive.archive_format not in {"zip", "tar.gz"}:
        issues.append(f"{label}: unreadable archive_format {archive.archive_format!r}")
    return issues


def _release_issues(label: str, release: Any, column_sets: set[str]) -> list[str]:
    """Flag one release whose declared shape is self-inconsistent."""
    issues = []
    if release.column_set not in column_sets:
        issues.append(f"{label}: unknown column_set {release.column_set!r}")
    if not release.files:
        issues.append(f"{label}: no archive files declared")
    for fmt, archive in release.files.items():
        issues.extend(_archive_issues(f"{label}[{fmt}]", archive))
    period = release.data_period
    if period is not None and period[0] > period[1]:
        issues.append(f"{label}: data_period {period} is inverted")
    return issues


def _row_issues(key: str, record: Any, column_sets: set[str]) -> list[str]:
    """Flag one extension whose releases are unpinned or self-inconsistent."""
    versions = getattr(record, "versions", None) or {}
    if not versions:
        return [f"{key}: no versions declared"]
    issues: list[str] = []
    if getattr(record, "default_version", "") not in versions:
        issues.append(
            f"{key}: default_version {record.default_version!r} is not among "
            f"{sorted(versions)}"
        )
    if not getattr(record, "license", ""):
        issues.append(f"{key}: no license recorded")
    if not getattr(record, "sources", None):
        issues.append(f"{key}: no source datasets recorded")
    for name, release in versions.items():
        issues.extend(_release_issues(f"{key}/{name}", release, column_sets))
    return issues


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each Caravan extension must pin a fetchable, self-consistent release.

    Args:
        catalog: The loaded Caravan `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    from earthlens.caravan.catalog import ColumnSet

    column_sets = set(ColumnSet.__args__)  # type: ignore[attr-defined]
    return lint(catalog, lambda k, r: _row_issues(k, r, column_sets))
