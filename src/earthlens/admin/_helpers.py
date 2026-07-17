"""Stateless URL-resolution and vector-read helpers for the admin backend.

These functions are the whole network-facing surface the backend routes through,
factored out so they can be unit-tested against captured fixtures without a
backend instance. Two rules from the plan are encoded here:

* **Every vector-file read goes through pyramids
  `FeatureCollection.read_file`** (`read_vector`), never a bare
  `geopandas.read_file` — reading a vector file is GIS I/O that belongs to the
  pyramids layer (porting policy).
* **Every result is normalised to EPSG:4326** (`read_vector`): TIGER arrives in
  NAD83 (EPSG:4269) and is reprojected; CGAZ arrives with an unlabelled
  geographic-degree CRS that is declared EPSG:4326 without a transform.

The GDAL virtual-filesystem prefixes (`/vsicurl/`, `/vsizip/`) pinned in the A1
gate are applied by the per-provider URL builders so the backend hands
`read_vector` a ready-to-open path.
"""

from __future__ import annotations

from typing import Any

import requests
from pyramids.feature.collection import FeatureCollection

from earthlens.base.http import HttpClient
from earthlens.base.http import RequestsGet as _RequestsGet

#: geoBoundaries gbOpen API base — `"{base}/{ISO3}/{ADM}/"` returns the metadata
#: whose `gjDownloadURL` is the GeoJSON to read (`geoboundaries_resolve`).
GEOBOUNDARIES_API = "https://www.geoboundaries.org/api/current/gbOpen"

#: CGAZ release tree base — one seamless GeoPackage per ADM level.
CGAZ_BASE = "https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/CGAZ"

#: Natural Earth NACIS CDN base — `"{base}/{scale}/cultural/ne_{scale}_{layer}.zip"`.
NE_BASE = "https://naciscdn.org/naturalearth"

#: US Census TIGER base — the GENZ cartographic-boundary (`cb_`) shapefile tree.
TIGER_BASE = "https://www2.census.gov/geo/tiger"
def vsicurl(url: str) -> str:
    """Wrap an HTTP(S) URL in GDAL's `/vsicurl/` virtual-filesystem prefix.

    Args:
        url: A plain `http(s)://` URL to a single vector file (GeoJSON / GPKG).

    Returns:
        str: The same URL prefixed with `/vsicurl/`, ready for
            `FeatureCollection.read_file`.
    """
    return f"/vsicurl/{url}"


def geoboundaries_resolve(iso: str, adm: str, timeout: float = 60.0) -> str:
    """Resolve a geoBoundaries country + ADM level to its GeoJSON download URL.

    The two-step geoBoundaries flow (`G5`): GET the gbOpen API metadata for the
    `(ISO3, ADM)` pair and return its `gjDownloadURL`. The endpoint returns a
    JSON object for a specific country + level, but the catch-all forms return a
    list — both are handled (a list takes its first entry).

    Args:
        iso: ISO-3166-1 alpha-3 country code (e.g. `"KEN"`), upper-cased by the
            caller / backend.
        adm: ADM level token (`"ADM0"` … `"ADM5"`).
        timeout: Per-request timeout in seconds for the metadata GET.

    Returns:
        str: The `gjDownloadURL` GeoJSON URL (a plain `https://` URL; wrap it in
            `vsicurl` before reading).

    Raises:
        requests.HTTPError: If the metadata endpoint returns a non-2xx status.
        ValueError: If the API returns an empty list (no boundary for the
            `(ISO, ADM)` pair).
        KeyError: If the metadata carries no `gjDownloadURL`.
    """
    http = HttpClient(
        session=_RequestsGet(),
        timeout=timeout,
        max_retries=0,
        status_forcelist=(),
        raise_for_status=True,
    )
    meta = http.get_json(f"{GEOBOUNDARIES_API}/{iso}/{adm}/")
    if isinstance(meta, list):
        if not meta:
            raise ValueError(
                f"geoBoundaries returned no boundary for {iso}/{adm}; the "
                "country may not publish that ADM level."
            )
        meta = meta[0]
    return meta["gjDownloadURL"]


def cgaz_url(level: str) -> str:
    """Build the `/vsicurl/` path to a CGAZ seamless GeoPackage for an ADM level.

    Args:
        level: ADM level token (`"ADM0"` / `"ADM1"` / `"ADM2"`).

    Returns:
        str: The `/vsicurl/`-prefixed GeoPackage URL for that level.
    """
    return vsicurl(f"{CGAZ_BASE}/geoBoundariesCGAZ_{level}.gpkg")


def natural_earth_url(scale: str, layer: str) -> str:
    """Build the `/vsizip//vsicurl/` path to a Natural Earth cultural layer ZIP.

    Args:
        scale: Natural Earth scale (`"10m"` / `"50m"` / `"110m"`).
        layer: Layer name fragment (`"admin_0_countries"`); the file stem is
            `ne_<scale>_<layer>`.

    Returns:
        str: The zipped-shapefile path, ready for `FeatureCollection.read_file`.
    """
    return f"/vsizip//vsicurl/{NE_BASE}/{scale}/cultural/ne_{scale}_{layer}.zip"


def tiger_url(year: int, entity: str, resolution: str, scope: str = "us") -> str:
    """Build the `/vsizip//vsicurl/` path to a TIGER cartographic-boundary ZIP.

    Args:
        year: Vintage year of the GENZ release (e.g. `2023`).
        entity: TIGER entity name (`"nation"` / `"state"` / `"county"` /
            `"tract"`).
        resolution: Cartographic-boundary resolution (`"500k"` / `"5m"` /
            `"20m"`).
        scope: `"us"` for nationwide entities, or a two-digit state FIPS code
            for per-state entities (e.g. `"06"` for California tracts).

    Returns:
        str: The zipped-shapefile path, ready for `FeatureCollection.read_file`.
    """
    file = f"cb_{year}_{scope}_{entity}_{resolution}.zip"
    return f"/vsizip//vsicurl/{TIGER_BASE}/GENZ{year}/shp/{file}"


def read_vector(url_or_path: str) -> FeatureCollection:
    """Read a vector file through pyramids and normalise it to EPSG:4326.

    The single read path for the admin backend (`G3` / `G8`): opens the file
    with `FeatureCollection.read_file` (GeoJSON / GeoPackage / zipped shapefile,
    plain path or GDAL `/vsi…/` path) and returns a `FeatureCollection` in
    EPSG:4326. The reprojection rule:

    * no CRS at all → assume the coordinates are WGS84 lon/lat and declare 4326;
    * already EPSG:4326 → returned unchanged;
    * a known non-4326 EPSG (e.g. TIGER's NAD83 / EPSG:4269) → reprojected
      in place (`to_crs(..., inplace=True)` keeps the `FeatureCollection` type);
    * a CRS that is present but unmappable to an EPSG yet geographic-degrees
      (CGAZ's "Undefined geographic SRS") → declared 4326 without a transform.

    Args:
        url_or_path: A `FeatureCollection.read_file` target — a local path or a
            GDAL `/vsicurl/` / `/vsizip//vsicurl/` path from the URL builders.

    Returns:
        FeatureCollection: The features, CRS EPSG:4326.
    """
    fc = FeatureCollection.read_file(url_or_path)
    if fc.crs is None:
        fc.set_crs("EPSG:4326", inplace=True, allow_override=True)
        return fc
    epsg = fc.crs.to_epsg()
    if epsg == 4326:
        return fc
    if epsg is not None:
        fc.to_crs("EPSG:4326", inplace=True)
        return fc
    # CRS present but unmappable (CGAZ's unlabelled geographic-degree SRS):
    # the coordinates are already lon/lat degrees, so declare 4326 rather than
    # attempt a transform from an unknown datum.
    fc.set_crs("EPSG:4326", inplace=True, allow_override=True)
    return fc


def empty_fc() -> FeatureCollection:
    """Return an empty EPSG:4326 `FeatureCollection` (zero polygon rows).

    Used as the fallback when a fetch yields nothing, so callers always get a
    `FeatureCollection` back rather than `None`.

    Returns:
        FeatureCollection: Zero rows, an empty `geometry` column, CRS
            EPSG:4326.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {}, geometry=gpd.GeoSeries([], crs="EPSG:4326"), crs="EPSG:4326"
    )
    return FeatureCollection(gdf)
