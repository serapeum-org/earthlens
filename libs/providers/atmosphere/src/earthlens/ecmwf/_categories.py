"""Auto-categorise a Copernicus dataset id into its per-family catalog shard.

`curate ecmwf --write` uses :func:`categorise_dataset` to pick which
per-family `catalog/<category>.yaml` shard a freshly seeded row belongs in,
so the maintainer no longer has to pass `--target` for the common case. The
rules are pure id-prefix tests (dataset ids across CDS / ADS / EWDS / ECDS /
XDS already encode their family in their name prefix), tried in order with
first match winning; an id matching no rule lands in `other` (the same shard
the `derived-*` / `sis-*` / `insitu-*` / `reanalysis-oras5*` /
`reanalysis-uerra-*` ids already sit in).

Prefixes must stay disjoint enough that a new rule cannot shadow an existing
one: `derived-fire-fuel` and `projections-fire-fuel` are deliberately narrower
than the bare `derived-` / `projections-` families so `derived-era5-*` and
`projections-cmip5-*` keep their current shards.
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
    "ecds",
    "xds",
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
    # ECMWF-hosted stores. `projections-fire-fuel` is listed ahead of the
    # `projections-cmip5` / `projections-cordex` rules for readability; the
    # three prefixes do not overlap, so order between them is not load-bearing.
    ("tigge-", "ecds"),
    ("s2s-", "ecds"),
    ("derived-fire-fuel", "xds"),
    ("projections-fire-fuel", "xds"),
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
    """Return the per-family shard stem for one dataset id.

    Args:
        dataset_id: A dataset id from any store — CDS / ADS / EWDS / ECDS / XDS
            (e.g. `reanalysis-era5-single-levels`, `cams-global-reanalysis-eac4`,
            `tigge-forecasts`).

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
        - The ECMWF-hosted stores get their own shards, and the narrow
          fire-fuel prefixes do not disturb the broader families they sit in:

            ```python
            >>> from earthlens.ecmwf._categories import categorise_dataset
            >>> categorise_dataset("tigge-forecasts")
            'ecds'
            >>> categorise_dataset("projections-fire-fuel-burned-area")
            'xds'
            >>> categorise_dataset("projections-cmip5-monthly-single-levels")
            'cmip5'

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
