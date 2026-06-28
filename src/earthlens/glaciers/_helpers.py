"""Stateless helpers for the glaciers backend.

Grouped by source:

* **Spatial / region mapping** — :func:`shapely_bbox` builds the AOI box that
  `SpatialExtent` does not carry, and :func:`regions_for_bbox` maps a request
  bbox to the overlapping GTN-G region id(s) (`G4` / `G6`).
* **Vector read (rgi/glims)** — :func:`download_zip` caches a region ZIP the
  ghsl way; :func:`read_outlines` reads the inner shapefile via pyramids
  `FeatureCollection.read_file("/vsizip/<zip>/<shp>")` and clips it to the bbox;
  :func:`fetch_glims` issues a GLIMS WFS bbox query, saves the GeoJSON, reads it
  via `FeatureCollection.read_file`, and clips. Vector file I/O always goes
  through pyramids — never a bare geopandas file read (`G3`).
  :func:`concat_outlines` merges per-region fragments.
* **Tabular read (wgms)** — :func:`parse_wgms_csv` reads one FoG table out of the
  cached zip with pandas (`G8`); :func:`wgms_glacier_table` reads the `glacier`
  join table (coordinates + region); :func:`empty_canonical` builds a schema-only
  frame. Pure pandas — no array/NetCDF stack.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import requests
from loguru import logger
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import box

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent


# --------------------------------------------------------------------------
# Spatial / region mapping (G4 / G6)
# --------------------------------------------------------------------------


def shapely_bbox(space: SpatialExtent) -> Any:
    """Build the AOI box a `SpatialExtent` does not carry (`G4`).

    `SpatialExtent` exposes only `north` / `south` / `east` / `west`, so the
    bbox intersect-filter needs this small adapter.

    Args:
        space: The request's :class:`~earthlens.base.SpatialExtent`.

    Returns:
        A `shapely.geometry.box(west, south, east, north)` polygon.
    """
    return box(space.west, space.south, space.east, space.north)


def regions_for_bbox(bbox: list[float], regions: dict[str, Any]) -> list[str]:
    """Map a request bbox to the overlapping GTN-G region id(s) (`G6`).

    Args:
        bbox: The request AOI as `[west, south, east, north]` in EPSG:4326.
        regions: The catalog region map (id -> a row exposing `.bboxes`, each
            a `[west, south, east, north]` quadruple).

    Returns:
        list[str]: The sorted ids of every region with a bbox intersecting the
            AOI. Empty when the AOI overlaps no glacier region.
    """
    aoi = box(*bbox)
    hits = []
    for region_id, region in regions.items():
        if any(box(*region_box).intersects(aoi) for region_box in region.bboxes):
            hits.append(region_id)
    return sorted(hits)


# --------------------------------------------------------------------------
# Download + cache (ghsl model)
# --------------------------------------------------------------------------


def download_zip(
    url: str,
    dest_dir: Path,
    *,
    session: requests.Session | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 180.0,
    chunk_size: int = 1 << 20,
) -> Path:
    """Stream a `.zip` to `dest_dir` and return the local path (idempotent).

    The glaciers vector / tabular paths read *inside* the zip (RGI via
    `/vsizip/`, WGMS via pandas), so — unlike the ghsl raster path — the archive
    is kept, not extracted. If the file already exists it is returned without
    re-downloading. Retries transient HTTP failures with exponential backoff.

    Args:
        url: The `.zip` URL (an IHP-WINS RGI region or the WGMS FoG archive).
        dest_dir: Directory to cache into (created if absent).
        session: Optional shared `requests.Session` for connection reuse.
        retries: Number of attempts before giving up.
        backoff: Base seconds for exponential backoff between retries.
        timeout: Per-request timeout in seconds.
        chunk_size: Streaming chunk size in bytes.

    Returns:
        pathlib.Path: The cached local `.zip` path.

    Raises:
        requests.HTTPError: If every attempt fails (the last error is raised).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / url.rsplit("/", 1)[-1]
    if zip_path.exists() and zip_path.stat().st_size > 0:
        return zip_path
    _stream_download(url, zip_path, session, retries, backoff, timeout, chunk_size)
    return zip_path


def _stream_download(
    url: str,
    dest_path: Path,
    session: requests.Session | None,
    retries: int,
    backoff: float,
    timeout: float,
    chunk_size: int,
) -> None:
    """Stream `url` to `dest_path` with retry + exponential backoff.

    Args:
        url: The source URL.
        dest_path: Local path to write.
        session: Optional shared session.
        retries: Attempts before giving up.
        backoff: Base backoff seconds.
        timeout: Per-request timeout.
        chunk_size: Streaming chunk size.

    Raises:
        requests.HTTPError: If every attempt fails (the last error re-raised).
    """
    get = session.get if session is not None else requests.get
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        handle.write(chunk)
            return
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            dest_path.unlink(missing_ok=True)
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"glaciers: download {url} failed (attempt "
                    f"{attempt}/{retries}): {type(exc).__name__}: {exc}; "
                    f"retrying in {wait:.0f}s."
                )
                time.sleep(wait)
    raise requests.HTTPError(
        f"glaciers: download {url} failed after {retries} attempts: {last_exc}"
    )


# --------------------------------------------------------------------------
# Vector read (rgi/glims) — always via pyramids FeatureCollection (G3)
# --------------------------------------------------------------------------


def _inner_shapefile(zip_path: Path) -> str:
    """Return the single `.shp` member name inside an RGI region zip.

    RGI zip member names are lowercase while the inner `.shp` is uppercase, so
    the name is resolved from the archive rather than derived from the zip name.

    Args:
        zip_path: A local RGI region `.zip`.

    Returns:
        str: The inner `.shp` member name.

    Raises:
        ValueError: If the zip has no — or more than one — `.shp` member.
    """
    with zipfile.ZipFile(zip_path) as zf:
        shps = [m for m in zf.namelist() if m.lower().endswith(".shp")]
    if len(shps) != 1:
        raise ValueError(
            f"expected exactly one .shp in {zip_path.name}, found {shps!r}"
        )
    return shps[0]


def _clip_to_bbox(fc: FeatureCollection, bbox: list[float]) -> FeatureCollection:
    """Reproject to EPSG:4326 if needed and clip a collection to the bbox (`G4`).

    pyramids `FeatureCollection` exposes no `crop`/`clip`, so the clip is the
    geopandas intersect-filter against `shapely.box(*bbox)`.

    Args:
        fc: The read collection.
        bbox: `[west, south, east, north]` in EPSG:4326.

    Returns:
        FeatureCollection: The features intersecting the bbox, in EPSG:4326.
    """
    if str(fc.crs).upper() not in ("EPSG:4326", "WGS84"):
        fc = fc.to_crs("EPSG:4326")
    clipped = fc[fc.intersects(box(*bbox))]
    return FeatureCollection(clipped)


def read_outlines(
    zip_path: Path, bbox: list[float], inner_shp: str | None = None
) -> FeatureCollection:
    """Read RGI outlines from a cached region zip and clip to the bbox (`G3`/`G4`).

    Reads the shapefile *inside* the region ZIP via pyramids
    `FeatureCollection.read_file("/vsizip/<zip>/<shp>")` — never a bare geopandas
    file read (porting policy) — reprojects to EPSG:4326 if needed, and clips to
    the bbox.

    Args:
        zip_path: The local RGI region `.zip` (from :func:`download_zip`).
        bbox: `[west, south, east, north]` AOI in EPSG:4326.
        inner_shp: The inner `.shp` member name; resolved from the zip when
            `None`.

    Returns:
        FeatureCollection: Glacier-outline polygons intersecting the bbox, in
            EPSG:4326.
    """
    shp = inner_shp or _inner_shapefile(zip_path)
    vsi = f"/vsizip/{Path(zip_path).resolve()}/{shp}"
    fc = FeatureCollection.read_file(vsi)
    return _clip_to_bbox(fc, bbox)


def glims_wfs_url(
    wfs_url: str, typename: str, bbox: list[float], max_features: int
) -> tuple[str, dict[str, Any]]:
    """Build the GLIMS WFS GetFeature request (URL + params) for a bbox query.

    Axis-order landmine: with the URN CRS `urn:ogc:def:crs:EPSG::4326` the WFS
    bbox is `south,west,north,east` (lat,lon); a plain `EPSG:4326` srs returns
    nothing. This builds the URN form.

    Args:
        wfs_url: The GLIMS GeoServer WFS endpoint.
        typename: The WFS feature-type name
            (`"GLIMS:GLIMS_Glacier_Outlines"`).
        bbox: `[west, south, east, north]` AOI in EPSG:4326.
        max_features: Cap on returned features (the WFS `count`).

    Returns:
        A `(url, params)` pair to pass to `requests.get`.
    """
    west, south, east, north = bbox
    crs_urn = "urn:ogc:def:crs:EPSG::4326"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": typename,
        "outputFormat": "application/json",
        "srsName": crs_urn,
        "count": str(max_features),
        "bbox": f"{south},{west},{north},{east},{crs_urn}",
    }
    return wfs_url, params


def fetch_glims(
    wfs_url: str,
    typename: str,
    bbox: list[float],
    dest_path: Path,
    *,
    max_features: int = 10000,
    session: requests.Session | None = None,
    timeout: float = 120.0,
) -> FeatureCollection:
    """Query the GLIMS WFS for a bbox, save the GeoJSON, read + clip it (`G3`).

    Issues a WFS GetFeature bbox query, writes the GeoJSON response to
    `dest_path`, reads it via pyramids `FeatureCollection.read_file` (not a bare
    geopandas file read), and clips to the bbox.

    Args:
        wfs_url: The GLIMS GeoServer WFS endpoint.
        typename: The WFS feature-type name.
        bbox: `[west, south, east, north]` AOI in EPSG:4326.
        dest_path: Local path to write the GeoJSON cache file.
        max_features: Cap on returned features (the WFS `count`).
        session: Optional shared `requests.Session`.
        timeout: Per-request timeout in seconds.

    Returns:
        FeatureCollection: GLIMS outline polygons intersecting the bbox, in
            EPSG:4326. Empty when the query matched nothing.

    Raises:
        requests.HTTPError: If the WFS returns a non-2xx status.
    """
    url, params = glims_wfs_url(wfs_url, typename, bbox, max_features)
    get = session.get if session is not None else requests.get
    resp = get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(resp.text, encoding="utf-8")
    fc = FeatureCollection.read_file(str(Path(dest_path).resolve()))
    if len(fc) == 0:
        return FeatureCollection(fc)
    return _clip_to_bbox(fc, bbox)


def empty_feature_collection() -> FeatureCollection:
    """Build an empty EPSG:4326 :class:`FeatureCollection` (no features).

    Returned by the vector path when a request's bbox overlaps no glacier
    region / matches no outline, so `download()` always yields a collection.

    Returns:
        FeatureCollection: A zero-feature collection in EPSG:4326.
    """
    import geopandas as gpd

    return FeatureCollection(gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))


def concat_outlines(fragments: list[FeatureCollection]) -> FeatureCollection:
    """Merge per-region outline fragments into one collection.

    Args:
        fragments: One :class:`FeatureCollection` per region (or query).

    Returns:
        FeatureCollection: The concatenated collection in EPSG:4326. The first
            non-empty fragment when only one carries features; an empty
            collection when every fragment is empty.

    Raises:
        ValueError: If `fragments` is empty.
    """
    if not fragments:
        raise ValueError("concat_outlines() needs at least one fragment")
    non_empty = [fc for fc in fragments if len(fc) > 0]
    if not non_empty:
        return FeatureCollection(fragments[0])
    if len(non_empty) == 1:
        return FeatureCollection(non_empty[0])
    merged = pd.concat(non_empty, ignore_index=True)
    return FeatureCollection(merged, crs=non_empty[0].crs)


# --------------------------------------------------------------------------
# Tabular read (wgms) — pure pandas, no array/NetCDF stack (G8)
# --------------------------------------------------------------------------


def parse_wgms_csv(zip_path: Path, table: str) -> pd.DataFrame:
    """Read one WGMS FoG table out of the cached zip (`G8`).

    The FoG tables are already long-format (`glacier_id` / year-or-date /
    value), so this is a plain `pandas.read_csv` of the `data/<table>.csv`
    member — no reshaping, no array/NetCDF stack.

    Args:
        zip_path: The cached WGMS FoG `.zip` (from :func:`download_zip`).
        table: The table name (`"mass_balance"`, `"front_variation"`,
            `"state"`).

    Returns:
        pandas.DataFrame: The table rows.

    Raises:
        KeyError: If the zip has no `data/<table>.csv` member.
    """
    member = f"data/{table}.csv"
    with zipfile.ZipFile(zip_path) as zf:
        if member not in zf.namelist():
            raise KeyError(
                f"WGMS FoG zip has no {member!r} member "
                f"(available: {sorted(zf.namelist())})"
            )
        with zf.open(member) as handle:
            return pd.read_csv(io.BytesIO(handle.read()), low_memory=False)


def wgms_glacier_table(zip_path: Path) -> pd.DataFrame:
    """Read the WGMS `glacier` join table (coordinates + region) from the zip.

    The `glacier` table carries `id` (== the other tables' `glacier_id`),
    `latitude` / `longitude`, and `gtng_region`, so it is the key for a
    region / bbox filter over a fluctuations table.

    Args:
        zip_path: The cached WGMS FoG `.zip`.

    Returns:
        pandas.DataFrame: The `glacier` table.

    Raises:
        KeyError: If the zip has no `data/glacier.csv` member.
    """
    return parse_wgms_csv(zip_path, "glacier")


def _as_list(value: Any) -> list[Any]:
    """Normalise a scalar / iterable selector into a list.

    Args:
        value: A scalar, list, tuple, or set.

    Returns:
        list: `value` as a list (a scalar becomes a one-element list).
    """
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def filter_wgms(
    df: pd.DataFrame,
    glaciers: pd.DataFrame | None = None,
    *,
    glacier_id: Any = None,
    glacier_name: str | None = None,
    region: Any = None,
    bbox: list[float] | None = None,
) -> pd.DataFrame:
    """Filter a WGMS fluctuations table by glacier / region / bbox.

    `glacier_id` and `glacier_name` match the table directly; `region` and
    `bbox` need the `glacier` join table (its `id` == the table's `glacier_id`,
    its `gtng_region` is id-prefixed like `"11_central_europe"`, and it carries
    `latitude` / `longitude`).

    Args:
        df: A fluctuations table with a `glacier_id` (and `glacier_name`) column.
        glaciers: The `glacier` join table; required for `region` / `bbox`.
        glacier_id: One id or a list of ids (matched against `glacier_id`).
        glacier_name: A case-insensitive substring matched against
            `glacier_name`.
        region: One GTN-G region id (`"11"`) or a list, matched against the
            `glacier` table's `gtng_region` prefix.
        bbox: `[west, south, east, north]` filtering glaciers by their point
            coordinates in the `glacier` table.

    Returns:
        pandas.DataFrame: The filtered rows, with a reset index.
    """
    out = df
    if glacier_id is not None:
        ids = {int(i) for i in _as_list(glacier_id)}
        out = out[out["glacier_id"].isin(ids)]
    if glacier_name is not None:
        names = out["glacier_name"].astype(str)
        out = out[names.str.contains(glacier_name, case=False, na=False)]
    if (region is not None or bbox is not None) and glaciers is not None:
        sel = glaciers
        if region is not None:
            wanted = {str(r) for r in _as_list(region)}
            prefix = sel["gtng_region"].astype(str).str.split("_").str[0]
            sel = sel[prefix.isin(wanted)]
        if bbox is not None:
            west, south, east, north = bbox
            sel = sel[
                sel["longitude"].between(west, east)
                & sel["latitude"].between(south, north)
            ]
        out = out[out["glacier_id"].isin(set(sel["id"]))]
    return out.reset_index(drop=True)


def empty_canonical(columns: list[str]) -> pd.DataFrame:
    """Build a schema-only (zero-row) frame with the given columns.

    Args:
        columns: The column names to materialise.

    Returns:
        pandas.DataFrame: An empty frame carrying exactly `columns`.
    """
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
