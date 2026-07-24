"""Map an Overture `GeoDataFrame` into a pyramids `FeatureCollection`.

This module is the only place in the Overture backend that assembles a
GIS vector container, so per the pyramids policy it keeps geometry / CRS
handling inside pyramids primitives: earthlens tags the SDK's
`GeoDataFrame` with its CRS, adds the per-row `license_id` column
(see `earthlens.overture._helpers`), optionally caps the row count, and
hands the result to
`pyramids.feature.collection.FeatureCollection`.

The Overture SDK returns a `GeoDataFrame` with **no CRS set** even though
the coordinates are WGS84 lon/lat, so `to_feature_collection` tags it
`EPSG:4326` before wrapping — otherwise a written file would carry no
projection and warn. Both the populated path (`to_feature_collection`)
and the empty path (`empty_fc`) yield a collection carrying at least an
`id` / `geometry` / `license_id` schema so a downstream `to_file` /
`to_parquet` never chokes on a hit-vs-miss schema mismatch.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from loguru import logger
from pyramids.feature.collection import FeatureCollection

from earthlens.overture._helpers import (
    CDLA_PERMISSIVE,
    derive_license_ids,
    warn_if_odbl,
)

#: WGS84 — the CRS every Overture FeatureCollection is tagged with. The
#: SDK omits it from the returned GeoDataFrame, so it is set explicitly.
OVERTURE_CRS = "EPSG:4326"

#: Minimal schema the empty-result collection carries (the populated path
#: adds Overture's full nested schema on top of these).
_EMPTY_COLUMNS = ["id", "license_id"]


def to_feature_collection(
    gdf: gpd.GeoDataFrame,
    label: str,
    max_features: int | None = None,
) -> FeatureCollection:
    """Tag CRS, add `license_id`, optionally cap rows, and wrap as a FeatureCollection.

    Args:
        gdf: The `GeoDataFrame` returned by the `overturemaps` SDK for one
            feature type. May have no CRS set (the SDK omits it).
        label: Short `"<theme>/<type>"` label for log / warning messages.
        max_features: Optional cap on rows kept; excess rows are dropped
            (head) with a warning. `None` keeps every row.

    Returns:
        FeatureCollection: The features tagged `EPSG:4326` with a
            `license_id` column added. An empty input yields a
            schema-correct empty collection.

    Examples:
        - A CRS-less frame is tagged EPSG:4326 and gains a `license_id`:
            ```python
            >>> import geopandas as gpd
            >>> from shapely.geometry import Point
            >>> from earthlens.overture.collection import to_feature_collection
            >>> gdf = gpd.GeoDataFrame(
            ...     {
            ...         "id": ["a"],
            ...         "sources": [[{"dataset": "Overture", "license": "CDLA-Permissive-2.0"}]],
            ...     },
            ...     geometry=[Point(0, 0)],
            ... )
            >>> fc = to_feature_collection(gdf, label="places/place")
            >>> fc["license_id"].iloc[0]
            'CDLA-Permissive-2.0'
            >>> fc.crs.to_epsg()
            4326

            ```
        - An empty input yields a schema-correct empty collection:
            ```python
            >>> import geopandas as gpd
            >>> from earthlens.overture.collection import to_feature_collection
            >>> empty = to_feature_collection(gpd.GeoDataFrame(), label="places/place")
            >>> len(empty)
            0
            >>> "license_id" in empty.columns
            True

            ```
    """
    if gdf is None or len(gdf) == 0:
        return empty_fc()

    if max_features is not None and len(gdf) > max_features:
        logger.warning(
            f"{label}: {len(gdf)} features fetched but max_features="
            f"{max_features}; keeping the first {max_features} and dropping "
            f"{len(gdf) - max_features}. Shrink the bbox for a complete, "
            "deterministic result."
        )
        gdf = gdf.head(max_features)

    if gdf.crs is None:
        gdf = gdf.set_crs(OVERTURE_CRS)

    license_ids = derive_license_ids(gdf)
    gdf = gdf.assign(license_id=license_ids)
    warn_if_odbl(license_ids, label)

    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the minimal Overture schema.

    Used for a query that matched nothing (an empty bbox) so callers
    always get a collection with an `id` / `geometry` / `license_id`
    schema back regardless of hit count.

    Returns:
        FeatureCollection: Zero rows, columns `id` / `license_id` plus an
            empty `geometry` column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.overture.collection import empty_fc
            >>> fc = empty_fc()
            >>> len(fc)
            0
            >>> "license_id" in fc.columns
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    frame = pd.DataFrame(
        {column: pd.Series([], dtype="object") for column in _EMPTY_COLUMNS}
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=OVERTURE_CRS), crs=OVERTURE_CRS
    )
    return FeatureCollection(gdf)


# Re-exported so callers can reference the default license without importing
# the private helpers module.
DEFAULT_LICENSE = CDLA_PERMISSIVE
