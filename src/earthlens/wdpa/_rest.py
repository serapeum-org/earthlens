"""Thin direct client for the Protected Planet (WDPA) v4 REST API.

The `pywdpa` package targets the retired v3 API and writes a shapefile to
disk, so the WDPA backend talks to Protected Planet v4 directly with
`requests` (a core dependency — no extra). Protected Planet v3 is taken
down on 2026-05-01; v4 is current and requires a personal token passed as a
`?token=` **query parameter** (not a Bearer header).

This module exposes two fetches that return a polygon `GeoDataFrame` in
`EPSG:4326`:

* :func:`fetch_country` loops `GET /v4/protected_areas/search` by ISO3
  country code (`per_page` max 50) until a short page, with
  `with_geometry=true` so each area carries its GeoJSON geometry.
* :func:`fetch_by_id` fetches one protected area by its WDPA id.

Point-only protected areas (a `Point` geometry rather than a polygon) are
dropped — the backend's contract is protected-area polygons. All geometry
assembly stays in geopandas/shapely so the backend just wraps the result in
a pyramids `FeatureCollection`.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

from earthlens.wdpa.auth import AuthenticationError

#: Base URL of the current Protected Planet API (v3 retires 2026-05-01).
BASE_URL = "https://api.protectedplanet.net/v4"

#: WGS84 — the CRS every WDPA FeatureCollection is tagged with.
CRS = "EPSG:4326"

#: Protected Planet caps `per_page` at 50 for the search endpoint.
PER_PAGE = 50

#: Polygon geometry types kept; point-only protected areas are dropped.
_POLYGON_TYPES = {"Polygon", "MultiPolygon"}

#: Attribute columns carried on every protected-area row.
WDPA_COLUMNS: dict[str, str] = {
    "wdpa_id": "string",
    "name": "string",
    "iso3": "string",
    "designation": "string",
    "iucn_category": "string",
    "marine": "boolean",
}


def _session(session: requests.Session | None) -> requests.Session:
    """Return the given session or a fresh `requests.Session`.

    Args:
        session: An existing session, or `None` to build one.

    Returns:
        requests.Session: The session to issue requests on.
    """
    if session is not None:
        return session
    return requests.Session()


def _get(session: requests.Session, path: str, params: dict[str, Any]) -> dict:
    """GET a v4 endpoint and return the parsed JSON, mapping 401 to auth error.

    Args:
        session: The HTTP session.
        path: Endpoint path under :data:`BASE_URL` (e.g.
            `"protected_areas/search"`).
        params: Query parameters (must include `token`).

    Returns:
        dict: The parsed JSON response body.

    Raises:
        AuthenticationError: On an HTTP 401 (missing/invalid token).
    """
    response = session.get(f"{BASE_URL}/{path}", params=params, timeout=60)
    if getattr(response, "status_code", None) == 401:
        raise AuthenticationError(
            "Protected Planet rejected the WDPA token (HTTP 401). Check "
            "WDPA_TOKEN / the token= argument, or request one at "
            "https://api.protectedplanet.net/request."
        )
    response.raise_for_status()
    return response.json()


def _row(area: dict) -> dict[str, Any] | None:
    """Map one protected-area JSON record to an attribute+geometry row.

    Args:
        area: One `protected_areas` JSON object with an embedded
            `geojson.geometry`.

    Returns:
        A row dict (the :data:`WDPA_COLUMNS` plus `geometry`), or `None`
        when the area has no geometry or a point-only geometry.
    """
    geojson = area.get("geojson") or {}
    geometry = geojson.get("geometry") or area.get("geometry")
    if not geometry or geometry.get("type") not in _POLYGON_TYPES:
        return None
    designation = area.get("designation") or {}
    category = area.get("iucn_category") or {}
    countries = area.get("countries") or []
    first_country = countries[0] if countries else None
    iso3 = (
        first_country.get("iso_3")
        if isinstance(first_country, dict)
        else first_country
    ) or area.get("iso3")
    return {
        "wdpa_id": str(area.get("wdpa_id") or area.get("id") or ""),
        "name": area.get("name"),
        "iso3": iso3,
        "designation": designation.get("name") if isinstance(designation, dict) else designation,
        "iucn_category": category.get("name") if isinstance(category, dict) else category,
        "marine": area.get("marine"),
        "geometry": shape(geometry),
    }


def _to_gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    """Assemble protected-area rows into a typed polygon GeoDataFrame.

    Args:
        rows: Row dicts from :func:`_row` (already polygon-filtered).

    Returns:
        gpd.GeoDataFrame: One row per protected area, CRS `EPSG:4326`,
            carrying exactly :data:`WDPA_COLUMNS` plus `geometry`. Empty
            input yields a schema-correct empty frame.
    """
    if not rows:
        frame = pd.DataFrame({c: pd.Series([], dtype=t) for c, t in WDPA_COLUMNS.items()})
        return gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs=CRS), crs=CRS)
    geometry = gpd.GeoSeries([r.pop("geometry") for r in rows], crs=CRS)
    frame = pd.DataFrame(rows, columns=list(WDPA_COLUMNS))
    for column, dtype in WDPA_COLUMNS.items():
        frame[column] = frame[column].astype(dtype)
    return gpd.GeoDataFrame(frame, geometry=geometry, crs=CRS)


def fetch_country(
    token: str,
    iso3: str,
    *,
    session: requests.Session | None = None,
    max_pages: int = 1000,
) -> gpd.GeoDataFrame:
    """Fetch every protected area for a country as a polygon GeoDataFrame.

    Loops `GET /v4/protected_areas/search?country=<iso3>&with_geometry=true`
    page by page (50/page) until a short page, dropping point-only areas.

    Args:
        token: The Protected Planet API token (`?token=` query param).
        iso3: ISO3 country code (e.g. `"KEN"`).
        session: Optional `requests.Session` (injected in tests).
        max_pages: Safety cap on the number of pages fetched.

    Returns:
        gpd.GeoDataFrame: The country's protected-area polygons, CRS
            `EPSG:4326`.

    Raises:
        AuthenticationError: On an HTTP 401.
    """
    http = _session(session)
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        params = {
            "token": token,
            "country": iso3,
            "with_geometry": "true",
            "per_page": PER_PAGE,
            "page": page,
        }
        areas = _get(http, "protected_areas/search", params).get("protected_areas") or []
        rows.extend(row for area in areas if (row := _row(area)) is not None)
        if len(areas) < PER_PAGE:
            break
        page += 1
    return _to_gdf(rows)


def fetch_by_id(
    token: str,
    wdpa_id: str,
    *,
    session: requests.Session | None = None,
) -> gpd.GeoDataFrame:
    """Fetch one protected area by WDPA id as a polygon GeoDataFrame.

    Args:
        token: The Protected Planet API token (`?token=` query param).
        wdpa_id: The WDPA id of the protected area.
        session: Optional `requests.Session` (injected in tests).

    Returns:
        gpd.GeoDataFrame: The protected area as a one-row (or empty, if
            point-only) GeoDataFrame, CRS `EPSG:4326`.

    Raises:
        AuthenticationError: On an HTTP 401.
    """
    http = _session(session)
    params = {"token": token, "with_geometry": "true"}
    body = _get(http, f"protected_areas/{wdpa_id}", params)
    area = body.get("protected_area") or body
    row = _row(area)
    return _to_gdf([row] if row is not None else [])
