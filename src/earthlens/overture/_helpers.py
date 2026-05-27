"""Per-row license provenance for the Overture backend.

Overture's `sources` column records, per feature, the upstream datasets
that contributed to it and the license each carries — a list of structs
`{property, dataset, license, record_id, update_time, confidence,
between}`. This module turns that into a single per-row `license_id`
string and warns when any `ODbL-1.0` (OSM-derived) rows are present.

This is the headline, novel piece of the Overture backend: no other
earthlens backend surfaces per-feature licensing, and it is what lets a
downstream commercial user tell apart the permissive bulk of Overture
(`CDLA-Permissive-2.0`) from the share-alike `ODbL-1.0` rows that carry
attribution / share-alike obligations.

Derivation rule (per row), in order:

1. If any source carries `ODbL-1.0` *or* names the `OpenStreetMap`
   dataset, the row is `ODbL-1.0`. The share-alike obligation dominates
   the combined feature, so it wins outright — this is what the
   `LicenseWarning` keys off, and it is why "first non-empty license" is
   *not* used (an ODbL source that is not listed first must still win).
2. Otherwise, if the sources carry explicit licenses, the row is the
   sorted, `"; "`-joined set of those distinct licenses (faithful to a
   feature built from multiple permissively-licensed sources, e.g.
   `Apache-2.0; CDLA-Permissive-2.0`).
3. Otherwise (no explicit license field — older releases), the row falls
   back to `CDLA-Permissive-2.0`, Overture's default.
"""

from __future__ import annotations

import warnings

import pandas as pd

#: Share-alike license OSM-derived rows carry; the value the warning keys off.
ODBL = "ODbL-1.0"

#: Overture's default permissive license for non-OSM-derived rows.
CDLA_PERMISSIVE = "CDLA-Permissive-2.0"

#: Dataset name that marks an OSM-derived source (the ODbL fallback trigger).
_OSM_DATASET = "OpenStreetMap"


class LicenseWarning(UserWarning):
    """Warns that a download contains share-alike (`ODbL-1.0`) rows.

    Emitted by `warn_if_odbl` when any feature's derived `license_id` is
    `ODbL-1.0`. `ODbL-1.0` (OSM-derived data) carries attribution and
    share-alike obligations that `CDLA-Permissive-2.0` does not, so a
    downstream commercial user must be told the obligation rides along
    with those rows rather than discovering it silently.
    """


def _coerce_sources(sources: object) -> list[dict]:
    """Normalise one row's `sources` cell into a list of struct dicts.

    The cell may be a numpy array of dicts (fresh from the SDK), a plain
    list (read back from GeoParquet), `None`, or a NaN scalar (a row with
    no sources). All non-list-like / missing values normalise to `[]`.

    Args:
        sources: One row's `sources` value.

    Returns:
        list[dict]: The source structs, or `[]` when the cell is empty /
            missing / not iterable.
    """
    if sources is None:
        return []
    if isinstance(sources, float) and pd.isna(sources):
        return []
    try:
        items = list(sources)
    except TypeError:
        return []
    return [item for item in items if isinstance(item, dict)]


def row_license(sources: object) -> str:
    """Derive one feature's `license_id` from its `sources` cell.

    Implements the module's three-step rule: ODbL (or OSM) wins; else the
    joined set of explicit licenses; else the `CDLA-Permissive-2.0`
    fallback.

    Args:
        sources: One row's `sources` value (a list of source structs, a
            numpy array of them, or a missing value).

    Returns:
        str: The derived license identifier — `"ODbL-1.0"`,
            `"CDLA-Permissive-2.0"`, or a `"; "`-joined set of explicit
            licenses.

    Examples:
        - An OSM-derived row is ODbL, even when listed second:
            ```python
            >>> from earthlens.overture._helpers import row_license
            >>> row_license([
            ...     {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
            ...     {"dataset": "OpenStreetMap", "license": "ODbL-1.0"},
            ... ])
            'ODbL-1.0'

            ```
        - Permissive sources are listed, sorted and joined:
            ```python
            >>> from earthlens.overture._helpers import row_license
            >>> row_license([
            ...     {"dataset": "Foursquare", "license": "Apache-2.0"},
            ...     {"dataset": "Overture", "license": "CDLA-Permissive-2.0"},
            ... ])
            'Apache-2.0; CDLA-Permissive-2.0'

            ```
        - No explicit license and not OSM falls back to CDLA-Permissive:
            ```python
            >>> from earthlens.overture._helpers import row_license
            >>> row_license([{"dataset": "Overture"}])
            'CDLA-Permissive-2.0'
            >>> row_license(None)
            'CDLA-Permissive-2.0'

            ```
    """
    items = _coerce_sources(sources)
    licenses = [s["license"] for s in items if s.get("license")]
    datasets = [s["dataset"] for s in items if s.get("dataset")]
    if ODBL in licenses or _OSM_DATASET in datasets:
        return ODBL
    if licenses:
        return "; ".join(sorted(set(licenses)))
    return CDLA_PERMISSIVE


def derive_license_ids(gdf: pd.DataFrame) -> pd.Series:
    """Return the per-row `license_id` series for an Overture `GeoDataFrame`.

    Applies `row_license` over the `sources` column. When the frame has
    no `sources` column (a projection that dropped it), every row falls
    back to `CDLA-Permissive-2.0`.

    Args:
        gdf: An Overture `GeoDataFrame` (or any frame with a `sources`
            column of source structs).

    Returns:
        pandas.Series: One `license_id` string per row, index-aligned to
            `gdf`.

    Examples:
        - Derive a per-row `license_id` from a `sources` column:
            ```python
            >>> import pandas as pd
            >>> from earthlens.overture._helpers import derive_license_ids
            >>> frame = pd.DataFrame(
            ...     {
            ...         "sources": [
            ...             [{"dataset": "OpenStreetMap", "license": "ODbL-1.0"}],
            ...             [{"dataset": "Overture", "license": "CDLA-Permissive-2.0"}],
            ...         ]
            ...     }
            ... )
            >>> list(derive_license_ids(frame))
            ['ODbL-1.0', 'CDLA-Permissive-2.0']

            ```
        - A frame with no `sources` column defaults every row to CDLA:
            ```python
            >>> import pandas as pd
            >>> from earthlens.overture._helpers import derive_license_ids
            >>> list(derive_license_ids(pd.DataFrame({"id": ["a", "b"]})))
            ['CDLA-Permissive-2.0', 'CDLA-Permissive-2.0']

            ```
    """
    if "sources" not in gdf.columns:
        return pd.Series(
            [CDLA_PERMISSIVE] * len(gdf), index=gdf.index, dtype="object"
        )
    return gdf["sources"].apply(row_license).astype("object")


def warn_if_odbl(license_ids: pd.Series, label: str) -> int:
    """Emit a `LicenseWarning` when any row carries `ODbL-1.0`.

    Args:
        license_ids: The per-row `license_id` series (from
            `derive_license_ids`).
        label: A short label naming the fetched theme/type, for the
            warning message (e.g. `"buildings/building"`).

    Returns:
        int: The number of `ODbL-1.0` rows found (0 emits no warning).

    Examples:
        - A permissive-only series emits nothing and counts zero:
            ```python
            >>> import pandas as pd
            >>> from earthlens.overture._helpers import warn_if_odbl
            >>> warn_if_odbl(pd.Series(["CDLA-Permissive-2.0", "Apache-2.0"]), "places/place")
            0

            ```
        - ODbL rows are counted (and a `LicenseWarning` is emitted):
            ```python
            >>> import pandas as pd
            >>> from earthlens.overture._helpers import warn_if_odbl
            >>> warn_if_odbl(pd.Series(["ODbL-1.0", "CDLA-Permissive-2.0"]), "buildings/building")
            1

            ```
    """
    odbl_count = int((license_ids == ODBL).sum())
    if odbl_count:
        warnings.warn(
            f"{label}: {odbl_count} of {len(license_ids)} feature(s) are "
            f"{ODBL} (OSM-derived). ODbL-1.0 carries attribution and "
            "share-alike obligations that CDLA-Permissive-2.0 does not; "
            "honour them when redistributing these rows.",
            LicenseWarning,
            stacklevel=2,
        )
    return odbl_count
