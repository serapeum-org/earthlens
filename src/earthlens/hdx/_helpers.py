"""Internal helpers for the HDX backend.

Currently just :func:`match_resource`, the resource-filter matcher
shared by :meth:`earthlens.hdx.backend.HDX._search` and the catalog's
default `resource_filter`. Kept out of `backend.py` so the matching
rule can be unit-tested in isolation.
"""

from __future__ import annotations

import fnmatch


def match_resource(name: str, fmt: str, resource_filter: str) -> bool:
    """Return whether one HDX resource matches a `resource_filter` (`G2`).

    HDX/CKAN has no spatial/temporal query, so resource selection is by
    name glob or CKAN format label. A resource matches when:

    * the filter is empty / whitespace — every resource matches; or
    * the resource `name` matches the filter as a shell glob
      (`"*.gpkg.gz"`, or a literal resource name); or
    * the filter equals the resource `format` label case-insensitively
      (a bare CKAN label such as `"Geopackage"`, `"CSV"`, `"GeoTIFF"`),
      with a leading `"*."` stripped so `"*.csv"` also matches the
      `"CSV"` format label.

    Args:
        name: The resource `name` (`r["name"]`), e.g.
            `"kontur_population_20231101.gpkg.gz"`.
        fmt: The resource CKAN format label (`r["format"]`), e.g.
            `"Geopackage"` — a label, not a file extension.
        resource_filter: The name glob or format label to match
            against. Empty matches everything.

    Returns:
        bool: `True` when the resource matches the filter.

    Examples:
        - A name glob matches on the resource name:
            ```python
            >>> from earthlens.hdx._helpers import match_resource
            >>> match_resource("kontur_pop.gpkg.gz", "Geopackage", "*.gpkg.gz")
            True

            ```
        - A bare format label matches on the format, not the name:
            ```python
            >>> from earthlens.hdx._helpers import match_resource
            >>> match_resource("export_2024.zip", "Geopackage", "geopackage")
            True

            ```
        - An empty filter keeps every resource:
            ```python
            >>> from earthlens.hdx._helpers import match_resource
            >>> match_resource("anything.bin", "CSV", "")
            True

            ```
    """
    needle = resource_filter.strip().lower()
    if not needle:
        return True
    if fnmatch.fnmatch(name.lower(), needle):
        return True
    return fmt.strip().lower() == needle.removeprefix("*.")
