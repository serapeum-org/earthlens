"""Map a GDACS GeoJSON feed into a pyramids `FeatureCollection`.

This module is the only place in the GDACS backend that touches a GIS
vector container, so per the pyramids policy it keeps all
geometry/CRS handling inside pyramids primitives: earthlens assembles
the plain attribute rows, builds the geometry column from each
feature's GeoJSON `geometry` via `shapely.geometry.shape`, and hands
the whole thing to
:class:`pyramids.feature.collection.FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) tagged `EPSG:4326`.

The canonical alert schema lives here as :data:`ATTRIBUTE_COLUMNS`
(attribute columns + dtypes) plus the `geometry` column. Both the
populated path (:func:`geojson_to_fc`) and the empty path
(:func:`empty_fc`) produce a FeatureCollection with exactly these
columns and dtypes, so a downstream `to_file` never chokes on a schema
mismatch between a hit and a miss.

Field mapping follows the live GDACS SEARCH feed. Every feature's
`properties` carries a flat, uniform `severitydata` sub-dict
(`{severity, severitytext, severityunit}`) regardless of hazard type,
so a single `severity` / `severity_unit` / `severity_text` trio covers
all six hazards. Properties are read defensively with `.get(...)` so a
renamed or absent field degrades to a null cell rather than raising —
the GDACS API is informally documented and has drifted historically.
The numeric columns (`alert_score`, `severity`) are coerced with
`pandas.to_numeric(errors="coerce")` rather than a hard cast, so a
non-numeric token (GDACS emits `""` for some absent fields) becomes
`NaN` instead of aborting the whole mapping.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.errors import ShapelyError
from shapely.geometry import shape

#: WGS84 — the CRS every GDACS alert FeatureCollection is tagged with.
EVENT_CRS = "EPSG:4326"

#: Ordered attribute columns and their pandas dtypes. The `geometry`
#: column is added separately by :func:`geojson_to_fc` / :func:`empty_fc`
#: and is not listed here.
ATTRIBUTE_COLUMNS: dict[str, str] = {
    "event_id": "string",
    "episode_id": "string",
    "hazard_type": "string",
    "name": "string",
    "alert_level": "string",
    "alert_score": "float64",
    "from_date": "datetime64[ns, UTC]",
    "to_date": "datetime64[ns, UTC]",
    "country": "string",
    "iso3": "string",
    "glide": "string",
    "severity": "float64",
    "severity_unit": "string",
    "severity_text": "string",
}

#: Columns parsed to tz-aware UTC datetimes rather than cast directly.
_DATE_COLUMNS = ("from_date", "to_date")


def geojson_to_fc(payload: dict[str, Any]) -> FeatureCollection:
    """Convert a GDACS GeoJSON FeatureCollection into a `FeatureCollection`.

    One row per feature, columns per :data:`ATTRIBUTE_COLUMNS` plus a
    `geometry` column built from each feature's GeoJSON `geometry`
    (`Point` for most alerts; floods / cyclone tracks may carry
    polygons or lines, which are preserved as-is). A feature with no
    usable geometry still contributes a row, but its `geometry` is
    `None` rather than an invalid value that would corrupt a written
    GeoPackage/GeoJSON. An empty (or geometry-less) payload returns an
    empty FeatureCollection with the same columns/dtypes (see
    :func:`empty_fc`). The numeric columns (`alert_score`, `severity`)
    are coerced with `pandas.to_numeric(errors="coerce")`, so a
    non-numeric value degrades to `NaN` rather than raising.

    Args:
        payload: The decoded GDACS SEARCH response — a GeoJSON
            FeatureCollection mapping with a `"features"` list.

    Returns:
        FeatureCollection: One feature per alert, CRS `EPSG:4326`; rows
            whose feature lacked a usable geometry carry a null
            geometry.

    Examples:
        - Map a one-feature payload and read back a field:
            ```python
            >>> payload = {
            ...     "type": "FeatureCollection",
            ...     "features": [
            ...         {
            ...             "type": "Feature",
            ...             "geometry": {"type": "Point", "coordinates": [12.5, 42.0]},
            ...             "properties": {
            ...                 "eventtype": "EQ",
            ...                 "eventid": 1541788,
            ...                 "name": "Earthquake in Italy",
            ...                 "alertlevel": "Green",
            ...                 "alertscore": 1,
            ...                 "fromdate": "2024-01-01T00:00:00",
            ...                 "todate": "2024-01-01T00:00:00",
            ...                 "severitydata": {
            ...                     "severity": 4.7,
            ...                     "severitytext": "Magnitude 4.7M",
            ...                     "severityunit": "M",
            ...                 },
            ...             },
            ...         }
            ...     ],
            ... }
            >>> from earthlens.gdacs.events import geojson_to_fc
            >>> fc = geojson_to_fc(payload)
            >>> len(fc)
            1
            >>> fc["event_id"].iloc[0]
            '1541788'
            >>> float(fc["severity"].iloc[0])
            4.7
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    features = payload.get("features") or []
    rows = [_feature_to_row(feature) for feature in features]
    if not rows:
        return empty_fc()

    frame = pd.DataFrame([row[0] for row in rows], columns=list(ATTRIBUTE_COLUMNS))
    for column, dtype in ATTRIBUTE_COLUMNS.items():
        if column in _DATE_COLUMNS:
            # Parse first (to_datetime infers a unit), then pin to the
            # declared resolution so a populated frame and empty_fc share
            # an identical dtype.
            frame[column] = pd.to_datetime(
                frame[column], utc=True, errors="coerce"
            ).astype(dtype)
        elif dtype == "float64":
            # Coerce rather than hard-cast: GDACS emits "" (or other
            # non-numeric tokens) for absent numeric fields, and a bare
            # astype("float64") would raise on those and abort the whole
            # download. Coercion degrades a bad value to NaN, honouring
            # the module's "a renamed/absent field becomes null, not an
            # error" contract.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = frame[column].astype(dtype)

    geometries = [row[1] for row in rows]
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries(geometries, crs=EVENT_CRS), crs=EVENT_CRS
    )
    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the canonical alert schema.

    Used for a query that matched nothing (a quiet window) so callers
    always get the same columns and dtypes back regardless of hit
    count — an empty result is a legitimate answer, not an error.

    Returns:
        FeatureCollection: Zero rows, the :data:`ATTRIBUTE_COLUMNS`
            columns with their declared dtypes, an empty `geometry`
            column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.gdacs.events import empty_fc, ATTRIBUTE_COLUMNS
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


def clip_to_bbox(
    collection: FeatureCollection,
    lat_lim: list[float],
    lon_lim: list[float],
) -> FeatureCollection:
    """Drop alerts whose geometry falls outside the WGS84 bbox.

    GDACS SEARCH has no documented server-side bbox filter, so spatial
    selection is done client-side here. Rows with a null geometry are
    dropped (they cannot be placed in the box). An empty result returns
    a schema-correct empty FeatureCollection.

    Args:
        collection: The mapped alerts to filter.
        lat_lim: `[lat_min, lat_max]` in degrees.
        lon_lim: `[lon_min, lon_max]` in degrees.

    Returns:
        FeatureCollection: Only the alerts intersecting the bbox, CRS
            `EPSG:4326`.
    """
    if not len(collection):
        return collection
    lat_min, lat_max = min(lat_lim), max(lat_lim)
    lon_min, lon_max = min(lon_lim), max(lon_lim)
    within = collection.cx[lon_min:lon_max, lat_min:lat_max]
    if not len(within):
        return empty_fc()
    return FeatureCollection(
        gpd.GeoDataFrame(within, geometry="geometry", crs=EVENT_CRS)
    )


def _feature_to_row(feature: dict[str, Any]) -> tuple[dict[str, object], object]:
    """Extract one attribute row and geometry from a GDACS GeoJSON feature.

    Reads every property defensively (`.get`) so a renamed or missing
    field becomes `None` (rendered as `<NA>` / `NaN` once typed). The
    flat `severitydata` sub-dict is unpacked into the `severity` /
    `severity_unit` / `severity_text` columns.

    Args:
        feature: One GeoJSON feature mapping (`geometry` + `properties`).

    Returns:
        A `(row, geometry)` pair: `row` is a dict keyed by
        :data:`ATTRIBUTE_COLUMNS` (no `geometry`); `geometry` is a
        shapely geometry or `None`.
    """
    properties = feature.get("properties") or {}
    severity = properties.get("severitydata") or {}
    event_id = properties.get("eventid")
    episode_id = properties.get("episodeid")
    row = {
        "event_id": str(event_id) if event_id is not None else None,
        "episode_id": str(episode_id) if episode_id is not None else None,
        "hazard_type": properties.get("eventtype"),
        "name": properties.get("name"),
        "alert_level": properties.get("alertlevel"),
        "alert_score": properties.get("alertscore"),
        "from_date": properties.get("fromdate"),
        "to_date": properties.get("todate"),
        "country": properties.get("country"),
        "iso3": properties.get("iso3"),
        "glide": properties.get("glide"),
        "severity": severity.get("severity"),
        "severity_unit": severity.get("severityunit"),
        "severity_text": severity.get("severitytext"),
    }
    return row, _geometry_of(feature)


def _geometry_of(feature: dict[str, Any]) -> object:
    """Build a shapely geometry from a feature's GeoJSON `geometry`.

    Args:
        feature: One GeoJSON feature mapping.

    Returns:
        A shapely geometry, or `None` when the feature has no usable
        `geometry` (so the row gets a null geometry rather than an
        error).
    """
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("coordinates"):
        return None
    try:
        return shape(geometry)
    except (ValueError, KeyError, TypeError, ShapelyError):
        return None
