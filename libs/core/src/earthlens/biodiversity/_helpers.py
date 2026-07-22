"""Shared geometry, mapping, and licensing helpers for the biodiversity cluster.

Three concerns are factored here so the `gbif` / `obis` / `wdpa` / `iucn`
backends stay thin and consistent:

* `wkt_from_bbox` builds the WKT polygon the occurrence/area APIs accept from a
  `SpatialExtent` (which has `.west/.south/.east/.north` but no `.wkt()`).
* `occurrences_to_fc` turns occurrence rows into a points `FeatureCollection`,
  mirroring `earthlens.fdsn.events.catalog_to_fc`/`empty_fc` so a written
  GeoParquet/GeoPackage never chokes on a schema mismatch between a hit and a
  miss, and a row with a missing coordinate gets a null geometry rather than an
  invalid `POINT (nan nan)`.
* `LicenseWarning` / `warn_license` flag results whose license carries
  attribution, non-commercial, or redistribution obligations.

All GIS handling stays inside the `pyramids` `FeatureCollection` container per
the repository's pyramids policy; earthlens only assembles the plain attribute
rows and the `Point` geometry column.
"""

from __future__ import annotations

import datetime as dt
import warnings
from collections.abc import Iterable, Mapping
from email.utils import parsedate_to_datetime
from typing import cast

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import Point, box

#: WGS84 — the CRS every occurrence/area FeatureCollection in the cluster carries.
CRS = "EPSG:4326"

#: Sentinel license labels for the two token-gated sources whose terms are
#: always restrictive (not per-record CC ids): Protected Planet's custom
#: UNEP-WCMC license and the IUCN Red List's CC-BY-NC terms.
WDPA_LICENSE = "WDPA (UNEP-WCMC)"
IUCN_LICENSE = "IUCN Red List (CC-BY-NC)"

#: License ids/labels that carry non-commercial or restricted-redistribution
#: obligations. The GBIF record value is underscore-spelled (`CC_BY_NC_4_0`);
#: OBIS rows use the hyphenated Creative Commons forms; WDPA/IUCN use the
#: sentinels above. ODbL stays the concern of Overture's own `warn_if_odbl`.
RESTRICTIVE_LICENSES: frozenset[str] = frozenset(
    {
        "CC_BY_NC_4_0",
        "CC-BY-NC-4.0",
        "CC-BY-NC",
        WDPA_LICENSE,
        IUCN_LICENSE,
    }
)


class LicenseWarning(UserWarning):
    """Warns that a downloaded result carries license obligations.

    Emitted by `warn_license` when a result's license is non-commercial
    (`CC-BY-NC`), share-alike, or otherwise restricts redistribution (the
    custom Protected Planet / IUCN Red List terms). A downstream commercial
    user must be told the obligation rides along with the data rather than
    discovering it silently.

    Promoted here from the Overture backend so every biodiversity source — and
    Overture — raises the same warning class; `earthlens.overture._helpers`
    re-exports it for backward compatibility.
    """


def wkt_from_bbox(space) -> str:
    """Build a counter-clockwise WKT polygon from a spatial extent's bbox.

    `SpatialExtent` exposes the bbox edges as `.west/.south/.east/.north` but
    has no `.wkt()`, so the cluster builds the `geometry=` filter the GBIF /
    OBIS / WDPA APIs accept here. `shapely.geometry.box` emits a
    counter-clockwise ring, which GBIF requires.

    Args:
        space: A spatial extent exposing `.west`, `.south`, `.east`, and
            `.north` float properties (typically `self.space` on a backend).

    Returns:
        A `POLYGON((...))` WKT string spanning the bounding box.

    Examples:
        - Build the WKT for a small box:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.biodiversity import wkt_from_bbox
            >>> extent = SpatialExtent.from_pairs(lat_lim=(10.0, 20.0), lon_lim=(0.0, 5.0))
            >>> wkt_from_bbox(extent)
            'POLYGON ((5 10, 5 20, 0 20, 0 10, 5 10))'

            ```
    """
    return cast("str", box(space.west, space.south, space.east, space.north).wkt)


def occurrences_to_fc(
    records: Iterable[Mapping] | pd.DataFrame,
    *,
    lat_field: str,
    lon_field: str,
    columns: Mapping[str, str],
) -> FeatureCollection:
    """Map occurrence rows to a points `FeatureCollection` (EPSG:4326).

    Accepts both shapes the cluster produces: a `list[dict]` of records (GBIF's
    `occ.search()["results"]`) or a `pandas.DataFrame` (the value `pyobis`'s
    `.execute()` returns). The output is one feature per row, restricted and
    ordered to `columns` with their declared dtypes, plus a `geometry` column
    of `shapely.Point(lon, lat)`. A row whose latitude or longitude is missing
    gets a null geometry rather than an invalid `POINT (nan nan)` that would
    corrupt a written file. An empty input yields an empty FeatureCollection
    carrying exactly `columns`, so the result type is identical whether or not
    the query matched anything.

    Args:
        records: Occurrence rows as a `list[dict]` / iterable of mappings, or a
            `pandas.DataFrame` already shaped one row per occurrence.
        lat_field: Name of the latitude column (e.g. `"decimalLatitude"`).
        lon_field: Name of the longitude column (e.g. `"decimalLongitude"`).
        columns: Ordered mapping of output column name to pandas dtype; the
            result carries exactly these attribute columns.

    Returns:
        FeatureCollection: One feature per row, CRS `EPSG:4326`; rows with a
            missing coordinate carry a null geometry.
    """
    frame = (
        records.copy()
        if isinstance(records, pd.DataFrame)
        else pd.DataFrame(list(records), columns=list(columns))
    )
    if frame.empty:
        return _empty_fc(columns)

    frame = frame.reindex(columns=list(columns))
    for column, dtype in columns.items():
        frame[column] = frame[column].astype(dtype)

    points = [
        Point(lon, lat) if pd.notna(lon) and pd.notna(lat) else None
        for lon, lat in zip(frame[lon_field], frame[lat_field])
    ]
    # Tag the GeoSeries with the frame's own index so geopandas does not align
    # a default RangeIndex against a non-default frame index (which would null
    # every geometry) — OBIS frames can carry a non-default index.
    geometry = gpd.GeoSeries(points, index=frame.index, crs=CRS)
    gdf = gpd.GeoDataFrame(frame, geometry=geometry, crs=CRS)
    return FeatureCollection(gdf)


def warn_license(license_id: str, label: str, *, detail: str | None = None) -> bool:
    """Emit a `LicenseWarning` when a result's license is restrictive.

    No-ops for permissive licenses (`CC0`, `CC-BY`) so a caller can pass every
    record's license unconditionally. `detail` appends a source-specific
    obligation to the message.

    Args:
        license_id: The license id/label on the result (e.g. `"CC_BY_NC_4_0"`,
            or one of `WDPA_LICENSE` / `IUCN_LICENSE`).
        label: A short source/dataset label for the message (e.g. `"gbif"`).
        detail: Optional source-specific obligation appended to the message.

    Returns:
        `True` if a warning was emitted, `False` otherwise.
    """
    if license_id not in RESTRICTIVE_LICENSES:
        return False
    message = (
        f"{label}: '{license_id}' carries non-commercial / restricted-redistribution "
        f"obligations"
    )
    if detail:
        # ASCII hyphen (not an em-dash) on purpose: this string is printed by
        # `warnings.warn` to stderr, which on a default Windows cp1252 console
        # would `UnicodeEncodeError` on `—`. Keep this ASCII.
        message += f" - {detail}"
    message += ". Honour attribution and do not redistribute without permission."
    warnings.warn(message, LicenseWarning, stacklevel=2)
    return True


def parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    RFC 9110 §10.2.3 allows either an integer number of seconds (`"7"`) or an
    HTTP-date (`"Fri, 31 Dec 2027 23:59:59 GMT"`). Both forms are handled; an
    unparseable value or `None` yields `None` so the caller can fall back to
    its own back-off strategy. A past HTTP-date clamps to `0.0` (don't sleep
    backwards in time).

    Shared between the IUCN and WDPA REST shims, which both use it to
    implement `429`-aware retry back-off.

    Args:
        value: The raw header value, or `None` if the response did not
            carry a `Retry-After` header.

    Returns:
        The wait in seconds (>= 0), or `None` if the value is missing or
        unparseable.

    Examples:
        - Integer seconds:
            ```python
            >>> from earthlens.biodiversity import parse_retry_after
            >>> parse_retry_after("7")
            7.0

            ```
        - A past HTTP-date clamps to zero:
            ```python
            >>> from earthlens.biodiversity import parse_retry_after
            >>> parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT")
            0.0

            ```
        - Missing / unparseable values yield `None`:
            ```python
            >>> from earthlens.biodiversity import parse_retry_after
            >>> parse_retry_after(None) is None
            True
            >>> parse_retry_after("not a date") is None
            True

            ```
    """
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = dt.datetime.now(tz=target.tzinfo)
    return max(0.0, (target - now).total_seconds())


def _empty_fc(columns: Mapping[str, str]) -> FeatureCollection:
    """Return an empty `FeatureCollection` carrying exactly `columns`.

    Args:
        columns: Ordered mapping of column name to pandas dtype.

    Returns:
        FeatureCollection: Zero rows, the `columns` with their dtypes, an empty
            `geometry` column, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame({c: pd.Series([], dtype=t) for c, t in columns.items()})
    gdf = gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs=CRS), crs=CRS)
    return FeatureCollection(gdf)
