"""Pure bbox + result-conversion helpers for the OpenStreetMap backend.

Three concerns are factored here so `osm/backend.py` only routes:

* the bbox-order helpers `bbox_swne` / `bbox_wsen` / `shapely_bbox` —
  `SpatialExtent` exposes the bbox as `.south/.west/.north/.east` but has no
  `bbox_swne` / `shapely_bbox` of its own, and the two protocols want the
  corners in *different* orders (`G3`): Overpass QL is `S,W,N,E`, the ohsome
  `bboxes` parameter is `W,S,E,N`.
* `overpy_to_gdf` — overpy returns parsed elements, not a `GeoDataFrame`, so
  geometry is built here (`G4`). Under the `out geom;` QL the backend uses,
  way coordinates ride on `way.attributes["geometry"]` (a list of `{lat, lon}`
  dicts), *not* on `way.nodes` (which raises `DataIncomplete` under
  `out geom`) — see the A1 gate captures.
* `to_fc` / `empty_fc` — wrap a `GeoDataFrame` into a pyramids
  `FeatureCollection` (`G7`), normalising the CRS to EPSG:4326. The ohsome
  path's `.as_dataframe()` is already a `GeoDataFrame`, so it goes straight to
  `to_fc`.
* `OhsomeResponseError` / `OhsomeUnavailableError` + `ohsome_http_status` /
  `ohsome_error_response` / `ohsome_response_is_non_json` / `ohsome_body_preview`
  — turn the `ohsome` SDK's opaque failure into a clear, typed, actionable error
  carrying the evidence a raw `JSONDecodeError` discards (`#930`): the HTTP
  status, the response `Content-Type`, and the first bytes of the body. The SDK
  exposes those inconsistently (see `ohsome_http_status`), so they are recovered
  from the exception chain here.

All GIS containerisation stays inside the pyramids `FeatureCollection` per the
repository's pyramids policy; earthlens only assembles the plain attribute rows
and the shapely geometry column. `LicenseWarning` (`G5`) is re-exported from
the shared biodiversity home so the backend imports the one warning class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import LineString, Point, Polygon, box

from earthlens.base import exception_chain, is_http_status, response_status

# `LicenseWarning` is shared across the ODbL / restrictive-license backends; it
# lives in the biodiversity cluster's helper module (overture re-exports the same
# class object) and is re-exported here so the backend imports it from its own
# subpackage.
from earthlens.biodiversity import LicenseWarning  # noqa: F401

if TYPE_CHECKING:
    import requests

    from earthlens.base import SpatialExtent

#: WGS84 — the CRS every OSM FeatureCollection is tagged with.
OSM_CRS = "EPSG:4326"

#: The minimal identity columns every overpy-built row carries, ahead of the
#: element's own tags.
_ID_COLUMNS = ["osm_id", "osm_type"]


#: Characters of a non-JSON ohsome body to surface in the error / log line —
#: enough to recognise an HTML rate-limit / maintenance / login page without
#: dumping the whole thing.
OHSOME_BODY_PREVIEW_CHARS = 200


class OhsomeResponseError(RuntimeError):
    """The ohsome endpoint returned a response earthlens could not use.

    Raised in place of a raw `JSONDecodeError` when `api.ohsome.org` answers with
    a body that is not the expected GeoJSON — a rate-limit / maintenance / error
    page, an empty body, or a redirect followed to a landing page. Carries the
    HTTP `status_code`, the response `content_type`, and a short `body_preview`
    so a caller (and the logs) can tell those cases apart, instead of guessing
    behind a decoder error (`#930`).

    Deliberately a plain `RuntimeError`, **not** an
    `earthlens.base.UpstreamUnavailableError`: this is a broad "response we could
    not use" catch-all, and its `OhsomeUnavailableError` subtype can carry a
    non-transient status (a `404` / `600` from a bad filter is an earthlens
    defect, not an outage). The transient-vs-real triage is the osm backend's own
    job (`ohsome_http_status` + the e2e `_skip_on_network` helper); subclassing
    the shared availability type would let `is_upstream_unavailable` mask those
    deliberate failures as skips.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        content_type: str | None = None,
        body_preview: str | None = None,
    ) -> None:
        """Store the readable message and the recovered response evidence.

        Args:
            message: Human-facing explanation — what happened and what to do.
            status_code: The HTTP status of the offending response, or `None`
                when it could not be recovered from the SDK error.
            content_type: The response `Content-Type` header, or `None`.
            body_preview: The first characters of the response body (decoded), or
                `None` when no response object was recoverable.
        """
        super().__init__(message)
        self.status_code = status_code
        self.content_type = content_type
        self.body_preview = body_preview


class OhsomeUnavailableError(OhsomeResponseError):
    """The public ohsome endpoint refused a request with a retry-worthy status.

    A specialisation of `OhsomeResponseError` for the throttle / block / outage
    case: raised when `api.ohsome.org` answers a `403` (its front proxy blocking
    / throttling this client — the endpoint is public and keyless, so it is never
    a credential problem), a `429`, or a `5xx` server-side outage that outlived
    the SDK's automatic retries. Carries the HTTP `status_code` so a caller can
    tell a transient public-endpoint unavailability apart from a genuine request
    error and back off rather than fail hard.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        content_type: str | None = None,
        body_preview: str | None = None,
    ) -> None:
        """Store the actionable message and the recovered response evidence.

        Args:
            message: Human-facing explanation — what happened and what to do.
            status_code: The HTTP status that triggered it (`403` / `429` /
                `5xx`), or `None` when it could not be recovered from the SDK
                error.
            content_type: The response `Content-Type` header, or `None`.
            body_preview: The first characters of the response body, or `None`.
        """
        super().__init__(
            message,
            status_code=status_code,
            content_type=content_type,
            body_preview=body_preview,
        )


def ohsome_http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status behind an `ohsome` SDK failure.

    The SDK exposes the status inconsistently. Usually it wraps the failure into
    an `OhsomeException` carrying `error_code` (recovered by the first check
    below). But on the HTML `403` an overloaded `api.ohsome.org` front proxy
    returns, its non-JSON-body handling can misfire (its
    `except json.decoder.JSONDecodeError` misses the `simplejson`-based
    `requests.exceptions.JSONDecodeError`) and leak a bare `JSONDecodeError`
    whose originating `requests.HTTPError` — and its `403` status — is only
    reachable through the exception chain. This walks `exc` and its `__cause__` /
    `__context__` predecessors and returns the first HTTP status it finds, so
    both shapes are handled.

    Args:
        exc: The exception raised by an `ohsome` SDK call.

    Returns:
        int | None: The HTTP status code, or `None` when none is discoverable.

    Examples:
        - An `OhsomeException`-like error exposes its `error_code` directly:
            ```python
            >>> from earthlens.osm import ohsome_http_status
            >>> class _Err(Exception):
            ...     error_code = 429
            >>> ohsome_http_status(_Err())
            429

            ```
    """
    for node in exception_chain(exc):
        error_code = getattr(node, "error_code", None)
        if is_http_status(error_code):
            return error_code
        status = response_status(node)
        if status is not None:
            return status
    return None


def ohsome_error_response(exc: Exception) -> requests.Response | None:
    """Best-effort `requests.Response` behind an `ohsome` SDK failure.

    The companion to `ohsome_http_status`: walks the same exception chain and
    returns the first `response` object carrying a real HTTP status — the
    `OhsomeException`'s `.response`, or the `requests.HTTPError.response` buried
    in `__context__`. Hands the caller the status, `Content-Type`, and body the
    raw decoder error discards (`#930`).

    Args:
        exc: The exception raised by an `ohsome` SDK call.

    Returns:
        requests.Response | None: The offending response, or `None` when none is
            recoverable from the chain.

    Examples:
        - The buried response is recovered, so its status is readable:
            ```python
            >>> import requests
            >>> from earthlens.osm import ohsome_error_response
            >>> resp = requests.Response()
            >>> resp.status_code = 503
            >>> err = requests.HTTPError("service unavailable")
            >>> err.response = resp
            >>> ohsome_error_response(err).status_code
            503

            ```
        - A bare transport error carries no response:
            ```python
            >>> import requests
            >>> from earthlens.osm import ohsome_error_response
            >>> ohsome_error_response(requests.ConnectionError("boom")) is None
            True

            ```
    """
    for node in exception_chain(exc):
        response = getattr(node, "response", None)
        if response is not None and is_http_status(
            getattr(response, "status_code", None)
        ):
            return cast("requests.Response", response)
    return None


def ohsome_response_is_non_json(exc: Exception) -> bool:
    """Return whether the failure is a JSON-decode of the ohsome response body.

    True when a `JSONDecodeError` (stdlib, `simplejson`, or the `requests`
    variant) appears anywhere in the exception chain — the signature of "the body
    was not JSON" (an HTML rate-limit / maintenance / error page, an empty body,
    or a redirect to a landing page), as opposed to a genuine ohsome error served
    *as* JSON.

    Args:
        exc: The exception raised by an `ohsome` SDK call.

    Returns:
        bool: `True` when a JSON-decode failure is in the chain.
    """
    # Match by class name to catch the stdlib, `simplejson`, and `requests`
    # variants without importing them, guarded by `ValueError` (every real
    # variant subclasses it) so an unrelated same-named class is not a false
    # positive.
    for node in exception_chain(exc):
        if isinstance(node, ValueError) and type(node).__name__ == "JSONDecodeError":
            return True
    return False


def ohsome_body_preview(
    response: requests.Response | None,
    limit: int = OHSOME_BODY_PREVIEW_CHARS,
) -> str | None:
    """Return the first `limit` characters of a response body, or `None`.

    Reads `response.text` defensively (the SDK has already consumed the body, so
    it is cached) and truncates it — the evidence `#930` asks to surface without
    dumping a whole error page.

    Args:
        response: The offending response, or `None`.
        limit: Maximum number of characters to return.

    Returns:
        str | None: The decoded body prefix, or `None` when there is no response
            or its body could not be decoded.
    """
    if response is None:
        return None
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - a body we cannot decode is simply no preview
        return None
    return text[:limit]


def bbox_swne(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the bbox in Overpass QL order `(south, west, north, east)`.

    Args:
        space: A spatial extent exposing `.south`, `.west`, `.north`, and
            `.east` float properties (typically `self.space` on a backend).

    Returns:
        tuple[float, float, float, float]: `(south, west, north, east)`.

    Examples:
        - Overpass wants `S,W,N,E`:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import bbox_swne
            >>> extent = SpatialExtent.from_pairs(lat_lim=(49.40, 49.42), lon_lim=(8.67, 8.71))
            >>> bbox_swne(extent)
            (49.4, 8.67, 49.42, 8.71)

            ```
    """
    return (space.south, space.west, space.north, space.east)


def bbox_wsen(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the bbox in ohsome `bboxes` order `(west, south, east, north)`.

    Args:
        space: A spatial extent exposing `.west`, `.south`, `.east`, and
            `.north` float properties.

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)`.

    Examples:
        - ohsome wants `W,S,E,N`:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import bbox_wsen
            >>> extent = SpatialExtent.from_pairs(lat_lim=(49.40, 49.42), lon_lim=(8.67, 8.71))
            >>> bbox_wsen(extent)
            (8.67, 49.4, 8.71, 49.42)

            ```
    """
    return (space.west, space.south, space.east, space.north)


def shapely_bbox(space: SpatialExtent) -> Polygon:
    """Return a shapely `box` for the extent (for any client-side geometry filter).

    Args:
        space: A spatial extent exposing `.west`, `.south`, `.east`, and
            `.north` float properties.

    Returns:
        shapely.geometry.Polygon: `box(west, south, east, north)`.

    Examples:
        - The box spans the requested corners:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.osm import shapely_bbox
            >>> extent = SpatialExtent.from_pairs(lat_lim=(0.0, 2.0), lon_lim=(0.0, 1.0))
            >>> shapely_bbox(extent).bounds
            (0.0, 0.0, 1.0, 2.0)

            ```
    """
    return box(space.west, space.south, space.east, space.north)


def _way_geometry(way) -> Polygon | LineString | None:
    """Build a way's geometry from its inline `out geom;` coordinates.

    overpy stores the `out geom;` coordinates on `way.attributes["geometry"]`
    (a list of `{"lat": Decimal, "lon": Decimal}` dicts); `way.nodes` is
    unavailable under that QL. A closed ring (first point equals last, at least
    four points) becomes a `Polygon`; two or more points otherwise become a
    `LineString`; fewer than two points yields `None` (the row is skipped).

    Args:
        way: An overpy `Way` (or any object with the same `attributes` shape).

    Returns:
        Polygon | LineString | None: The way geometry, or `None` when there
            are too few coordinates to build one.
    """
    geom = way.attributes.get("geometry") or []
    coords = [(float(point["lon"]), float(point["lat"])) for point in geom]
    if len(coords) >= 4 and coords[0] == coords[-1]:
        return Polygon(coords)
    if len(coords) >= 2:
        return LineString(coords)
    return None


def _row(osm_id: int, osm_type: str, geometry, tags: dict) -> dict:
    """Build one element row with the reserved identity/geometry keys winning.

    The element's free-form OSM `tags` are spread first so a tag whose key
    collides with a reserved column (`osm_id`, `osm_type`, `geometry`) cannot
    clobber the identity or geometry value — the reserved keys are written last.

    Args:
        osm_id: The OSM element id.
        osm_type: `"node"` or `"way"`.
        geometry: The shapely geometry built for the element.
        tags: The element's OSM tags.

    Returns:
        dict: The row mapping, reserved keys last.
    """
    return {**dict(tags), "osm_id": osm_id, "osm_type": osm_type, "geometry": geometry}


def overpy_to_gdf(result: Any) -> gpd.GeoDataFrame:
    """Build a WGS84 `GeoDataFrame` from a parsed overpy result.

    overpy returns parsed `.nodes` / `.ways` / `.relations` rather than a
    `GeoDataFrame`, so geometry is assembled here (`G4`). Nodes become
    `Point(lon, lat)`; ways become a `Polygon` (closed ring) or `LineString`
    from their inline `out geom;` coordinates (see `_way_geometry`). Each
    element's `tags` are spread as columns alongside `osm_id` / `osm_type`.
    Relations are skipped in the MVP (member-geometry assembly is out of
    scope; documented in the OSM docs).

    Args:
        result: A parsed overpy result exposing `.nodes`, `.ways`, and
            `.relations` (e.g. from `overpy.Overpass().parse_json(...)`).

    Returns:
        geopandas.GeoDataFrame: One row per node/way with a built geometry,
            CRS `EPSG:4326`. Empty (schema-only) when nothing matched.

    Examples:
        - One node and one closed way become a Point and a Polygon:
            ```python
            >>> from types import SimpleNamespace as NS
            >>> from earthlens.osm import overpy_to_gdf
            >>> node = NS(id=1, lat=49.41, lon=8.69, tags={"amenity": "cafe"})
            >>> way = NS(
            ...     id=2,
            ...     tags={"building": "yes"},
            ...     attributes={"geometry": [
            ...         {"lat": 0.0, "lon": 0.0}, {"lat": 0.0, "lon": 1.0},
            ...         {"lat": 1.0, "lon": 1.0}, {"lat": 0.0, "lon": 0.0},
            ...     ]},
            ... )
            >>> result = NS(nodes=[node], ways=[way], relations=[])
            >>> gdf = overpy_to_gdf(result)
            >>> sorted(gdf.geometry.geom_type)
            ['Point', 'Polygon']
            >>> str(gdf.crs)
            'EPSG:4326'

            ```
    """
    rows: list[dict] = []
    for node in result.nodes:
        rows.append(
            _row(
                node.id,
                "node",
                Point(float(node.lon), float(node.lat)),
                node.tags,
            )
        )
    for way in result.ways:
        geometry = _way_geometry(way)
        if geometry is None:
            continue
        rows.append(_row(way.id, "way", geometry, way.tags))
    if not rows:
        return _empty_gdf()
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=OSM_CRS)


def to_fc(gdf: gpd.GeoDataFrame) -> FeatureCollection:
    """Wrap a `GeoDataFrame` as a `FeatureCollection`, normalising CRS to EPSG:4326.

    Used for both protocols (`G7`): the ohsome path's `.as_dataframe()` is
    already a `GeoDataFrame`, and the Overpass path's `overpy_to_gdf` builds
    one. A frame whose CRS is unset is tagged EPSG:4326; a frame in another CRS
    is reprojected.

    Args:
        gdf: The features to wrap (EPSG:4326, another CRS, or no CRS set).

    Returns:
        FeatureCollection: The features in EPSG:4326.

    Examples:
        - A CRS-less frame is tagged EPSG:4326:
            ```python
            >>> import geopandas as gpd
            >>> from shapely.geometry import Point
            >>> from earthlens.osm import to_fc
            >>> gdf = gpd.GeoDataFrame({"osm_id": [1]}, geometry=[Point(0, 0)])
            >>> fc = to_fc(gdf)
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    if gdf.crs is None:
        gdf = gdf.set_crs(OSM_CRS)
    elif gdf.crs.to_epsg() != 4326:
        # Compare by EPSG code, not string: a 4326-equivalent CRS expressed
        # differently (OGC:CRS84, a WKT-built CRS) should not trigger a
        # redundant reprojection.
        gdf = gdf.to_crs(OSM_CRS)
    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the minimal OSM schema.

    Used for a query that matched nothing, so callers always get a collection
    with an `osm_id` / `osm_type` / `geometry` schema back regardless of hit
    count.

    Returns:
        FeatureCollection: Zero rows, columns `osm_id` / `osm_type` plus an
            empty `geometry` column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.osm import empty_fc
            >>> fc = empty_fc()
            >>> len(fc)
            0
            >>> "osm_type" in fc.columns
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    return FeatureCollection(_empty_gdf())


def _empty_gdf() -> gpd.GeoDataFrame:
    """Return an empty WGS84 `GeoDataFrame` carrying the minimal OSM schema.

    Returns:
        geopandas.GeoDataFrame: Zero rows, columns `osm_id` / `osm_type`, an
            empty `geometry` column, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame(
        {column: pd.Series([], dtype="object") for column in _ID_COLUMNS}
    )
    return gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs=OSM_CRS), crs=OSM_CRS)
