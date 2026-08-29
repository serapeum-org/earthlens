"""Catalog-tooling handlers for the JRC backend (EFHM + sea-level forecasts).

Registered with core's `datasets validate` command through the `earthlens.cli`
entry-point group (see `earthlens._hazards_cli`).
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import lint, require

#: Required catalog fields per JRC dataset `kind` — the offline lint checks each
#: row carries the fields its access path needs.
_REQUIRED_BY_KIND: dict[str, tuple[str, ...]] = {
    "flood_hazard_raster": ("band", "base_url", "filename_template", "return_periods"),
    "sea_level_gridded": (
        "base_url",
        "product",
        "cycle_path_template",
        "gridded_glob",
        "default_field",
    ),
    "sea_level_coastal": ("base_url", "product", "cycle_path_template", "coastal_glob"),
}


def _check_row(key: str, row: Any) -> list[str]:
    """Lint one JRC row against the fields its `kind` requires."""
    fields = _REQUIRED_BY_KIND.get(getattr(row, "kind", ""))
    if fields is None:
        return [f"{key}: unknown kind {getattr(row, 'kind', None)!r}"]
    return require(key, row, fields)


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Lint each JRC row against the fields its `kind` needs.

    `flood_hazard_raster` (EFHM) needs the return-period GeoTIFF fields;
    `sea_level_gridded` needs the cycle-path + gridded-glob fields; and
    `sea_level_coastal` needs the cycle-path + coastal-glob fields.

    Args:
        catalog: The loaded JRC `Catalog`.

    Returns:
        `(checked, issues)` — the entry count and any structural problems.
    """
    return lint(catalog, _check_row)
