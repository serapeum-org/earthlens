"""Catalog-tooling handlers for the Tropycal backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The prober samples a live
season via the tropycal SDK; the validator diffs the catalog against the SDK's
supported basin/source universe offline.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require

#: tropycal's basin universe and which sources serve each (no `jtwc` source;
#: `both` is HURDAT NA+EP, `all` is IBTrACS global). Ported from the retired
#: `tools/tropycal/audit_tropycal_catalog.py`.
_SDK_BASIN_SOURCES: dict[str, list[str]] = {
    "north_atlantic": ["ibtracs", "hurdat"],
    "east_pacific": ["ibtracs", "hurdat"],
    "both": ["hurdat"],
    "west_pacific": ["ibtracs"],
    "north_indian": ["ibtracs"],
    "south_indian": ["ibtracs"],
    "australia": ["ibtracs"],
    "south_pacific": ["ibtracs"],
    "south_atlantic": ["ibtracs"],
    "all": ["ibtracs"],
}


def _tropycal_fields(basin: str, source: str) -> dict[str, dict[str, Any]]:
    """Return a basin's `Storm.to_dataframe()` field schema (samples a season)."""
    import datetime as dt

    import tropycal.tracks as tracks

    track_dataset = tracks.TrackDataset(basin=basin, source=source)
    year = dt.datetime.now(dt.UTC).year - 1
    storm_ids = list(track_dataset.get_season(year).summary().get("id") or [])[:3]
    fields: dict[str, dict[str, Any]] = {}
    for storm_id in storm_ids:
        frame = track_dataset.get_storm(storm_id).to_dataframe(attrs_as_columns=True)
        for column in frame.columns:
            fields.setdefault(str(column), {"dtype": str(frame[column].dtype)})
    return fields


def prober(catalog: Any, dataset: str) -> dict[str, dict[str, Any]]:
    """Probe a Tropycal basin's live `to_dataframe()` field schema (SDK).

    Args:
        catalog: The loaded Tropycal `Catalog` (resolves the basin's sources).
        dataset: A basin code (e.g. `north_atlantic`).

    Returns:
        Mapping of field name to `{dtype}`.
    """
    record = catalog.datasets.get(dataset)
    sources = getattr(record, "sources", None) or ["hurdat"]
    return _tropycal_fields(dataset, sources[0])


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each Tropycal basin needs a source, and must match the SDK universe.

    Beyond the per-row `sources` requirement, this diffs the catalog against
    tropycal's supported basin/source universe (the offline check the retired
    `audit_tropycal_catalog.py` ran): a curated basin tropycal no longer
    serves, a tropycal basin missing from the catalog, or a declared
    `(basin, source)` pair tropycal does not support.

    Args:
        catalog: The loaded Tropycal `Catalog`.

    Returns:
        `(checked, issues)`.
    """
    checked, issues = lint(catalog, lambda k, r: require(k, r, ("sources",)))
    catalog_basins = set(catalog.datasets)
    sdk_basins = set(_SDK_BASIN_SOURCES)
    issues += [
        f"{code}: basin not in tropycal's supported universe"
        for code in sorted(catalog_basins - sdk_basins)
    ]
    issues += [
        f"{code}: tropycal basin missing from the catalog"
        for code in sorted(sdk_basins - catalog_basins)
    ]
    for code in sorted(catalog_basins & sdk_basins):
        declared = set(getattr(catalog.datasets[code], "sources", None) or [])
        for bad in sorted(declared - set(_SDK_BASIN_SOURCES[code])):
            issues.append(f"{code}: source {bad!r} not supported by tropycal")
    return checked, issues
