"""Catalog-tooling handlers for the FDSN backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`). The refresh axis is obspy's
`URL_MAPPINGS` registry — the same source the catalog is curated from — so a
diff surfaces data centres obspy has gained or dropped.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import curated_attr_ids, lint, require

#: Curated-id resolver over each row's `fdsn_id` (the `audit` drift axis).
curated_ids = curated_attr_ids("fdsn_id")


def _provider_ids() -> list[str]:
    """Return every FDSN provider id obspy can reach (`URL_MAPPINGS` keys)."""
    from obspy.clients.fdsn.header import URL_MAPPINGS

    return [str(name) for name in URL_MAPPINGS]


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every FDSN provider id obspy can reach (SDK enum, no network).

    Args:
        catalog: The loaded FDSN `Catalog` (unused; obspy is the source).

    Returns:
        A single-group mapping `{"fdsn": [sorted provider ids]}`.
    """
    return {"fdsn": sorted(set(_provider_ids()))}


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each FDSN network needs an fdsn_id.

    Args:
        catalog: The loaded FDSN `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, lambda k, r: require(k, r, ("fdsn_id",)))
