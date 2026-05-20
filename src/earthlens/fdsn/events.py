"""Map an `obspy` event catalog into a pyramids `FeatureCollection`.

This module is the only place in the FDSN backend that touches a GIS
vector container, so per the pyramids policy it keeps all
geometry/CRS handling inside pyramids primitives: earthlens assembles
the plain attribute rows, builds a `Point` geometry column from the
origin longitude/latitude, and hands the whole thing to
:class:`pyramids.feature.collection.FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) tagged `EPSG:4326`.

The canonical event schema lives here as :data:`ATTRIBUTE_COLUMNS`
(attribute columns + dtypes) plus the `geometry` column. Both the
populated path (:func:`catalog_to_fc`) and the empty path
(:func:`empty_fc`) produce a FeatureCollection with exactly these
columns and dtypes, so a downstream `pandas.concat` / `to_file` never
chokes on a schema mismatch between a hit and a miss.

The unit conventions follow the FDSN event standard as obspy exposes
it: `origin.depth` is in **metres** (divided by 1000 for `depth_km`),
`origin.time` is a `UTCDateTime` (converted to a tz-aware UTC
`datetime64`), and `preferred_origin()` / `preferred_magnitude()` can
return `None`, so the first list element is used as a fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection

if TYPE_CHECKING:
    from obspy.core.event import Catalog, Event


#: WGS84 — the CRS every FDSN event FeatureCollection is tagged with.
EVENT_CRS = "EPSG:4326"

#: Ordered attribute columns and their pandas dtypes. The `geometry`
#: column (a `shapely.Point` per row) is added separately by
#: :func:`catalog_to_fc` / :func:`empty_fc` and is not listed here.
ATTRIBUTE_COLUMNS: dict[str, str] = {
    "event_id": "string",
    "time": "datetime64[ns, UTC]",
    "longitude": "float64",
    "latitude": "float64",
    "depth_km": "float64",
    "magnitude": "float64",
    "magnitude_type": "string",
    "event_type": "string",
    "status": "string",
    "provider": "string",
}


def catalog_to_fc(catalog: Catalog, provider: str) -> FeatureCollection:
    """Convert an obspy `Catalog` into a `FeatureCollection` of events.

    One row per event, columns per :data:`ATTRIBUTE_COLUMNS` plus a
    `geometry` column of `shapely.Point(longitude, latitude)` in
    `EPSG:4326`. An empty catalog returns an empty FeatureCollection
    with the same columns/dtypes (see :func:`empty_fc`) so the result
    type is identical whether or not the query matched anything.

    Args:
        catalog: An `obspy.core.event.Catalog` (iterable over
            `Event`), typically the return value of
            `obspy.clients.fdsn.Client.get_events`.
        provider: The user-facing provider key (`"USGS"`, `"EMSC"`,
            …) recorded in the `provider` column of every row.

    Returns:
        FeatureCollection: One feature per event, CRS `EPSG:4326`.

    Examples:
        - Map a one-event catalog and read back a field:
            ```python
            >>> from obspy.core.event import Catalog, Event, Origin, Magnitude
            >>> from obspy import UTCDateTime
            >>> origin = Origin(
            ...     time=UTCDateTime("2024-01-01T00:00:00"),
            ...     longitude=12.5, latitude=42.0, depth=10000.0,
            ...     evaluation_status="reviewed",
            ... )
            >>> magnitude = Magnitude(mag=5.2, magnitude_type="Mw")
            >>> event = Event(
            ...     origins=[origin], magnitudes=[magnitude],
            ...     event_type="earthquake",
            ... )
            >>> from earthlens.fdsn.events import catalog_to_fc
            >>> fc = catalog_to_fc(Catalog(events=[event]), "USGS")
            >>> len(fc)
            1
            >>> float(fc["depth_km"].iloc[0])
            10.0
            >>> fc["provider"].iloc[0]
            'USGS'
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    rows = [_event_to_row(event, provider) for event in catalog]
    if not rows:
        return empty_fc()

    frame = pd.DataFrame(rows, columns=list(ATTRIBUTE_COLUMNS))
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    for column, dtype in ATTRIBUTE_COLUMNS.items():
        if column == "time":
            continue
        frame[column] = frame[column].astype(dtype)

    geometry = gpd.points_from_xy(frame["longitude"], frame["latitude"], crs=EVENT_CRS)
    gdf = gpd.GeoDataFrame(frame, geometry=geometry, crs=EVENT_CRS)
    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the canonical event schema.

    Used for a query that matched nothing (a quiet region/time, an
    HTTP-204 `FDSNNoDataException`) so callers always get the same
    columns and dtypes back regardless of hit count — an empty result
    is a legitimate answer, not an error.

    Returns:
        FeatureCollection: Zero rows, the :data:`ATTRIBUTE_COLUMNS`
            columns with their declared dtypes, an empty `geometry`
            column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.fdsn.events import empty_fc, ATTRIBUTE_COLUMNS
            >>> fc = empty_fc()
            >>> len(fc)
            0
            >>> set(ATTRIBUTE_COLUMNS).issubset(fc.columns)
            True
            >>> "geometry" in fc.columns
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    frame = pd.DataFrame(
        {
            column: pd.Series([], dtype=dtype)
            for column, dtype in ATTRIBUTE_COLUMNS.items()
        }
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=EVENT_CRS), crs=EVENT_CRS
    )
    return FeatureCollection(gdf)


def concat_fcs(collections: list[FeatureCollection]) -> FeatureCollection:
    """Concatenate per-provider FeatureCollections into one.

    Skips empty inputs; returns :func:`empty_fc` when every input is
    empty (or the list is empty) so the result is always a
    schema-correct FeatureCollection.

    Args:
        collections: Per-provider FeatureCollections, each produced by
            :func:`catalog_to_fc` (so all share the schema and CRS).

    Returns:
        FeatureCollection: The row-wise union, CRS `EPSG:4326`.
    """
    non_empty = [fc for fc in collections if len(fc)]
    if not non_empty:
        return empty_fc()
    merged = pd.concat(non_empty, ignore_index=True)
    return FeatureCollection(
        gpd.GeoDataFrame(merged, geometry="geometry", crs=EVENT_CRS)
    )


def _event_to_row(event: Event, provider: str) -> dict[str, object]:
    """Extract one attribute row from an obspy `Event`.

    Uses `preferred_origin()` / `preferred_magnitude()` with a
    fallback to the first list element, converts `origin.depth`
    (metres) to kilometres, and stringifies the obspy
    `ResourceIdentifier`. A field whose source is absent becomes
    `None` (rendered as `<NA>` / `NaN` once typed).

    Args:
        event: One `obspy.core.event.Event`.
        provider: The provider key to stamp on the row.

    Returns:
        A dict keyed by :data:`ATTRIBUTE_COLUMNS` (no `geometry`).
    """
    origin = event.preferred_origin() or (event.origins[0] if event.origins else None)
    magnitude = event.preferred_magnitude() or (
        event.magnitudes[0] if event.magnitudes else None
    )
    depth = getattr(origin, "depth", None) if origin is not None else None
    origin_time = getattr(origin, "time", None) if origin is not None else None
    return {
        "event_id": str(event.resource_id),
        "time": origin_time.datetime if origin_time is not None else None,
        "longitude": getattr(origin, "longitude", None) if origin else None,
        "latitude": getattr(origin, "latitude", None) if origin else None,
        "depth_km": (depth / 1000.0) if depth is not None else None,
        "magnitude": magnitude.mag if magnitude is not None else None,
        "magnitude_type": (magnitude.magnitude_type if magnitude is not None else None),
        "event_type": event.event_type,
        "status": getattr(origin, "evaluation_status", None) if origin else None,
        "provider": provider,
    }
