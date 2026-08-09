"""Geometry helpers for the NSI backend.

Pure functions that turn a `[lat_lim, lon_lim]` bounding box into the two
spatial request shapes the vector sources need — a GeoJSON polygon body for the
NSI structures POST, and an ArcGIS envelope query for the FEMA NFHL layer — and
that wrap a returned GeoJSON `FeatureCollection` into a pyramids
:class:`~pyramids.feature.collection.FeatureCollection`. No network, no state.
"""

from __future__ import annotations

import json

import geopandas as gpd
from pyramids.feature.collection import FeatureCollection


def bbox_from_limits(
    lat_lim: list[float], lon_lim: list[float]
) -> tuple[float, float, float, float]:
    """Return `(xmin, ymin, xmax, ymax)` from `lat_lim` / `lon_lim`.

    Args:
        lat_lim: `[min_lat, max_lat]` in degrees.
        lon_lim: `[min_lon, max_lon]` in degrees.

    Returns:
        tuple[float, float, float, float]: The envelope as
            `(min_lon, min_lat, max_lon, max_lat)`.

    Raises:
        ValueError: If either limit is not a two-element `[min, max]` with
            `min < max`.

    Examples:
        ```python
        >>> from earthlens.nsi import bbox_from_limits
        >>> bbox_from_limits([29.95, 29.96], [-90.07, -90.06])
        (-90.07, 29.95, -90.06, 29.96)

        ```
    """
    if not (lat_lim and lon_lim and len(lat_lim) == 2 and len(lon_lim) == 2):
        raise ValueError(
            "lat_lim and lon_lim must each be a two-element [min, max]; got "
            f"lat_lim={lat_lim!r}, lon_lim={lon_lim!r}."
        )
    ymin, ymax = float(lat_lim[0]), float(lat_lim[1])
    xmin, xmax = float(lon_lim[0]), float(lon_lim[1])
    # Allow a degenerate (min == max) axis — a point / zero-width AOI — to match
    # the base SpatialExtent; only an inverted bound (min > max) is an error.
    if ymin > ymax or xmin > xmax:
        raise ValueError(
            f"each bound needs min <= max; got lat_lim={lat_lim!r}, lon_lim={lon_lim!r}."
        )
    return xmin, ymin, xmax, ymax


def nsi_polygon_body(lat_lim: list[float], lon_lim: list[float]) -> dict:
    """Build the GeoJSON-polygon body for an NSI structures POST.

    The NSI `?bbox=` query returns empty; the working AOI selector is a POST of
    a GeoJSON `FeatureCollection` carrying one rectangle polygon.

    Args:
        lat_lim: `[min_lat, max_lat]` in degrees.
        lon_lim: `[min_lon, max_lon]` in degrees.

    Returns:
        dict: A GeoJSON `FeatureCollection` mapping with a single closed
            rectangle `Polygon`, ready to pass as the JSON request body.

    Examples:
        - Build a polygon body for a small box:
            ```python
            >>> from earthlens.nsi import nsi_polygon_body
            >>> body = nsi_polygon_body([29.95, 29.96], [-90.07, -90.06])
            >>> body["type"]
            'FeatureCollection'
            >>> body["features"][0]["geometry"]["type"]
            'Polygon'
            >>> len(body["features"][0]["geometry"]["coordinates"][0])
            5

            ```
    """
    xmin, ymin, xmax, ymax = bbox_from_limits(lat_lim, lon_lim)
    ring = [
        [xmin, ymin],
        [xmax, ymin],
        [xmax, ymax],
        [xmin, ymax],
        [xmin, ymin],
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }


def arcgis_envelope(
    lat_lim: list[float], lon_lim: list[float], out_fields: str = "*"
) -> dict:
    """Build the ArcGIS `query` parameters for the NFHL flood-zone layer.

    Args:
        lat_lim: `[min_lat, max_lat]` in degrees.
        lon_lim: `[min_lon, max_lon]` in degrees.
        out_fields: Comma-separated attribute list to return (`"*"` = all).

    Returns:
        dict: The `params` for a GET against the layer's `query` endpoint —
            an `esriGeometryEnvelope` in WGS84, GeoJSON output, geometry on.

    Examples:
        - Build the query params for a box:
            ```python
            >>> from earthlens.nsi import arcgis_envelope
            >>> params = arcgis_envelope([29.95, 29.96], [-90.07, -90.06])
            >>> params["geometryType"]
            'esriGeometryEnvelope'
            >>> params["f"]
            'geojson'
            >>> params["outFields"]
            '*'

            ```
    """
    xmin, ymin, xmax, ymax = bbox_from_limits(lat_lim, lon_lim)
    envelope = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
    return {
        "geometry": json.dumps(envelope),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true",
        "f": "geojson",
        "where": "1=1",
    }


def to_feature_collection(geojson: dict) -> FeatureCollection:
    """Wrap a GeoJSON `FeatureCollection` mapping into a pyramids collection.

    Args:
        geojson: A GeoJSON mapping carrying a `features` list (WGS84).

    Returns:
        FeatureCollection: The features tagged `EPSG:4326`. An empty/missing
            `features` list yields a schema-light empty collection.

    Raises:
        ValueError: If `geojson` carries no `features` key at all.

    Examples:
        - Wrap a one-feature GeoJSON and inspect the collection:
            ```python
            >>> from earthlens.nsi import to_feature_collection
            >>> fc = to_feature_collection(
            ...     {
            ...         "type": "FeatureCollection",
            ...         "features": [
            ...             {
            ...                 "type": "Feature",
            ...                 "geometry": {"type": "Point", "coordinates": [-90.0, 29.9]},
            ...                 "properties": {"occtype": "RES1"},
            ...             }
            ...         ],
            ...     }
            ... )
            >>> len(fc)
            1

            ```
    """
    if "features" not in geojson:
        raise ValueError(
            "to_feature_collection expects a GeoJSON mapping with a 'features' "
            f"key; got keys {sorted(geojson)}."
        )
    features = geojson["features"]
    if not features:
        # An empty result carries just an (empty) geometry column — no fabricated
        # attribute/id column that a populated result (built from the provider's
        # properties) would not also have.
        empty = gpd.GeoDataFrame(
            geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326"
        )
        return FeatureCollection(empty)
    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    return FeatureCollection(gdf)
