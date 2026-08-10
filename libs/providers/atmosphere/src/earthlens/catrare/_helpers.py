"""Private, stateless helpers for the CatRaRE backend.

Three concerns live here so :class:`~earthlens.catrare.backend.CatRaRE` stays a
thin orchestration layer: the **FileGDB read + reprojection** (reading one layer
of the downloaded `.gdb.zip` through pyramids and reprojecting it from the DWD
RADOLAN grid — which the file does not carry — to EPSG:4326), the
**FeatureCollection assembly** (selecting the event attribute columns), and the
**date / bbox filter**. All geometry handling stays inside pyramids
(:class:`~pyramids.feature.collection.FeatureCollection`, itself a
`geopandas.GeoDataFrame` subclass); this module never imports `xarray`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from pyramids.feature.collection import FeatureCollection

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

#: WGS84 — the CRS the returned events are reprojected to.
FEATURE_CRS = "EPSG:4326"


def read_events(
    zip_path: Path, layer: str, source_crs: str
) -> FeatureCollection:
    """Read one FileGDB layer and reproject it to EPSG:4326.

    The CatRaRE FileGDB carries no embedded CRS; its geometry is in the DWD
    RADOLAN polar-stereographic grid. This reads the layer through pyramids,
    assigns `source_crs`, and reprojects to WGS84.

    Args:
        zip_path: The downloaded `.gdb.zip` on disk.
        layer: The FileGDB layer name to read.
        source_crs: The proj4 / WKT string the geometry is actually in.

    Returns:
        FeatureCollection: The layer's features, reprojected to EPSG:4326.
    """
    source = FeatureCollection.read_file(f"/vsizip/{zip_path}", layer=layer)
    return FeatureCollection(source.set_crs(source_crs, allow_override=True).to_crs(FEATURE_CRS))


def build_feature_collection(
    source: FeatureCollection, event_columns: list[str]
) -> FeatureCollection:
    """Select the event attribute columns (+ geometry) from the read layer.

    Args:
        source: The reprojected layer from :func:`read_events`.
        event_columns: The attribute columns to keep.

    Returns:
        FeatureCollection: The trimmed collection, CRS `EPSG:4326`.

    Raises:
        ValueError: If the layer lacks an expected event column (a clean domain
            error rather than a raw pandas `KeyError`), listing what is
            available.
    """
    missing = [column for column in event_columns if column not in source.columns]
    if missing:
        raise ValueError(
            f"the CatRaRE layer is missing expected column(s) {missing}; "
            f"available columns: {sorted(source.columns)}."
        )
    keep = [*event_columns, source.geometry.name]
    return FeatureCollection(source[keep])


def filter_events(
    collection: FeatureCollection,
    space: SpatialExtent,
    start: datetime | None,
    end: datetime | None,
) -> FeatureCollection:
    """Filter the events by date window and/or the requested bounding box.

    An event is kept when its `[Date_START, Date_END]` interval overlaps the
    requested `[start, end]` window (a half-open bound is honoured — pass only
    `start` or only `end` to bound one side). A bounding box narrower than the
    whole globe keeps events whose geometry intersects it. Both filters compose.

    Args:
        collection: The trimmed collection from :func:`build_feature_collection`.
        space: The requested :class:`~earthlens.base.SpatialExtent`; a
            whole-globe extent applies no spatial filter.
        start: Inclusive window start, or `None` for no lower bound.
        end: Inclusive window end, or `None` for no upper bound.

    Returns:
        FeatureCollection: The filtered events, CRS `EPSG:4326`.
    """
    result = collection
    if start is not None or end is not None:
        event_start = pd.to_datetime(result["Date_START"], errors="coerce")
        event_end = pd.to_datetime(result["Date_END"], errors="coerce")
        mask = pd.Series(True, index=result.index)
        if start is not None:
            mask &= event_end >= pd.Timestamp(start)
        if end is not None:
            mask &= event_start <= pd.Timestamp(end)
        result = result[mask]
    if not _is_global(space):
        result = result.cx[
            space.longitude_min : space.longitude_max,
            space.latitude_min : space.latitude_max,
        ]
    return FeatureCollection(result.set_crs(FEATURE_CRS, allow_override=True))


def _is_global(space: SpatialExtent) -> bool:
    """Return whether `space` is (effectively) the whole globe — no bbox filter.

    Args:
        space: The requested spatial extent.

    Returns:
        bool: `True` when the box spans the full WGS84 range.
    """
    return (
        space.latitude_min <= -90.0
        and space.latitude_max >= 90.0
        and space.longitude_min <= -180.0
        and space.longitude_max >= 180.0
    )
