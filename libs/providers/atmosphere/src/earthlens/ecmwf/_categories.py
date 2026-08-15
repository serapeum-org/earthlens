"""Auto-categorise a Copernicus dataset id into its per-family catalog shard.

`curate ecmwf --write` uses :func:`categorise_dataset` to pick which
per-family `catalog/<category>.yaml` shard a freshly seeded row belongs in,
so the maintainer no longer has to pass `--target` for the common case. The
rules are pure id-prefix tests (CDS/ADS/EWDS dataset ids already encode their
family in their name prefix), tried in order with first match winning; an id
matching no rule lands in `other` (the same shard the `derived-*` / `sis-*` /
`insitu-*` / `reanalysis-oras5*` / `reanalysis-uerra-*` ids already sit in).
"""

from __future__ import annotations

# All shards we emit, in display / file-order. Anything not matched by the
# rules table lands in `other`.
CATEGORIES = [
    "era5",
    "carra",
    "cerra",
    "cmip5",
    "cordex",
    "seasonal",
    "satellite",
    "ads",
    "ewds",
    "efas",
    "fire",
    "other",
]


# Each rule is (id_prefix, category); an id starting with the prefix routes to
# that shard. Rules are tried in order — the first match wins, so more specific
# prefixes (e.g. `cems-fire` before `cems-glofas`) come first.
_RULES: list[tuple[str, str]] = [
    ("cams-", "ads"),
    ("cems-fire", "fire"),
    ("cems-glofas", "ewds"),
    ("cems-flood", "ewds"),
    ("efas-", "efas"),
    ("reanalysis-pan-carra", "carra"),
    ("reanalysis-carra", "carra"),
    ("reanalysis-cerra", "cerra"),
    ("projections-cmip5", "cmip5"),
    ("projections-cordex", "cordex"),
    ("satellite-", "satellite"),
    ("seasonal-", "seasonal"),
    ("reanalysis-era5", "era5"),
]


def categorise_dataset(dataset_id: str) -> str:
    """Return the per-family shard stem for one Copernicus dataset id.

    Args:
        dataset_id: The CDS / ADS / EWDS dataset id (e.g.
            `reanalysis-era5-single-levels`, `cams-global-reanalysis-eac4`).

    Returns:
        The shard file stem the row belongs in (one of :data:`CATEGORIES`);
        `other` when the id matches no prefix rule.

    Examples:
        - An ERA5 id routes to the `era5` shard:

            ```python
            >>> from earthlens.ecmwf._categories import categorise_dataset
            >>> categorise_dataset("reanalysis-era5-single-levels")
            'era5'

            ```
        - A CAMS id routes to the `ads` shard:

            ```python
            >>> from earthlens.ecmwf._categories import categorise_dataset
            >>> categorise_dataset("cams-global-reanalysis-eac4")
            'ads'

            ```
        - An unrecognised id falls through to `other`:

            ```python
            >>> from earthlens.ecmwf._categories import categorise_dataset
            >>> categorise_dataset("reanalysis-oras5")
            'other'

            ```
    """
    for prefix, category in _RULES:
        if dataset_id.startswith(prefix):
            return category
    return "other"
