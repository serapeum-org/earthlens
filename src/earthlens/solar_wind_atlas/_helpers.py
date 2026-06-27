"""Stateless helpers for the Solar & Wind Atlas backend.

Two transports, one windowed-extraction core. The Global Wind Atlas layers are
range-accessible Cloud-Optimized GeoTIFFs on figshare, read **windowed** over
`/vsicurl/` so only the AOI bytes transfer (`window_crop`). The Global Solar
Atlas layers are DEFLATE-compressed ZIP archives with no random access, so they
are downloaded once into a cache and read **windowed from the local ZIP member**
(`download_cache_crop`). Both paths funnel through `read_part_to_geotiff`, which
uses `pyramids.dataset.Dataset.read_part` — the cloud-native partial read — and
never `Dataset.crop` (which materialises the whole global raster).

All raster I/O goes through `pyramids`; the stdlib `zipfile` is used only to
name the GeoTIFF member inside a downloaded ZIP, never to read pixels.
"""

from __future__ import annotations

import math
import os
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

#: GDAL `/vsicurl` HTTP settings applied (via `setdefault`) before the first
#: remote read. `GDAL_DISABLE_READDIR_ON_OPEN` stops GDAL listing the "directory"
#: of a remote object; the retry knobs ride out the figshare presigned-URL
#: refresh. `CPL_VSIL_CURL_ALLOWED_EXTENSIONS` is deliberately **not** set — it
#: whitelists extensions, and the figshare download URL is extension-less, so
#: setting it would make GDAL reject the URL (pinned in the A1b gate).
_GDAL_HTTP_ENV: dict[str, str] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
}

#: Stream chunk size (bytes) for the Global Solar Atlas ZIP download.
_DOWNLOAD_CHUNK = 1 << 20


def configure_gdal_http() -> None:
    """Apply the `/vsicurl` HTTP environment defaults (idempotent).

    Sets each key in `_GDAL_HTTP_ENV` only when absent, so a caller that has
    already tuned GDAL is left untouched. Called once at the top of every
    windowed read.
    """
    for key, value in _GDAL_HTTP_ENV.items():
        os.environ.setdefault(key, value)


def vsicurl(url: str) -> str:
    """Wrap a remote COG URL as a GDAL `/vsicurl/` path.

    Args:
        url: The `http(s)://` URL of a Cloud-Optimized GeoTIFF.

    Returns:
        str: The `/vsicurl/<url>` path GDAL reads with HTTP range requests.

    Examples:
        - The URL is prefixed verbatim:
            ```python
            >>> from earthlens.solar_wind_atlas._helpers import vsicurl
            >>> vsicurl("https://x/GHI.tif")
            '/vsicurl/https://x/GHI.tif'

            ```
    """
    return f"/vsicurl/{url}"


def bbox_from_extent(space: object) -> list[float]:
    """Return the `[west, south, east, north]` bbox of a spatial extent.

    Args:
        space: A `SpatialExtent` (the backend's `self.space`) exposing
            `west` / `south` / `east` / `north`.

    Returns:
        list[float]: `[west, south, east, north]` in degrees (EPSG:4326).
    """
    return [space.west, space.south, space.east, space.north]


def _source_no_data(dataset: object) -> object:
    """Return the source raster's first-band no-data value, or `-9999` default.

    `pyramids` exposes `no_data_value` as a per-band tuple (e.g. `(-9999.0,)` or
    `(None,)`). The windowed crop carries the source value through to the written
    GeoTIFF so genuinely-empty cells stay flagged; a missing value falls back to
    the pyramids default.

    Args:
        dataset: An opened `pyramids.Dataset`.

    Returns:
        The first-band no-data value when the source declares one, else `-9999`.
    """
    nodata = getattr(dataset, "no_data_value", None)
    if isinstance(nodata, (list, tuple)) and nodata and nodata[0] is not None:
        return nodata[0]
    return -9999


def read_part_to_geotiff(
    path: str, bbox: list[float], out_path: Path, *, epsg: int = 4326
) -> Path:
    """Read just the `bbox` window from a raster and write it as a GeoTIFF.

    The windowed primitive is `pyramids.dataset.Dataset.read_part`, which fetches
    only the AOI's byte ranges (for a COG over `/vsicurl/`, a few hundred KB
    rather than the whole multi-GB file). `Dataset.crop` is **not** used: it
    reads the entire band into memory, which for a global 250 m COG is tens of
    GiB. The returned pixels are wrapped back into a georeferenced GeoTIFF using
    the source grid snapped to the bbox.

    Args:
        path: A `/vsicurl/<url>` (remote COG) or `/vsizip/<zip>/<member.tif>`
            (downloaded ZIP member) path pyramids can open.
        bbox: `[west, south, east, north]` window in `epsg`.
        out_path: Destination GeoTIFF path.
        epsg: CRS of `bbox`. Defaults to `4326` (WGS84).

    Returns:
        Path: `out_path`, the written window GeoTIFF.
    """
    from pyramids.dataset import Dataset

    configure_gdal_http()
    west, south, east, north = bbox
    dataset = Dataset.read_file(path)
    try:
        origin_x, pixel_w, _, origin_y, _, pixel_h = dataset.geotransform
        col0 = math.floor((west - origin_x) / pixel_w)
        col1 = math.ceil((east - origin_x) / pixel_w)
        row0 = math.floor((north - origin_y) / pixel_h)
        row1 = math.ceil((south - origin_y) / pixel_h)
        # A point / sub-pixel bbox (e.g. lat_min == lat_max landing on a cell
        # edge) snaps to a zero-width window; read at least one pixel so the
        # request still yields a 1x1 cell instead of failing.
        ncols = max(1, col1 - col0)
        nrows = max(1, row1 - row0)
        array = dataset.read_part(
            (west, south, east, north),
            dst_width=ncols,
            dst_height=nrows,
            bbox_crs=epsg,
        )
        if getattr(array, "ndim", 2) == 3 and array.shape[0] == 1:
            array = array[0]
        geo = (
            origin_x + col0 * pixel_w,
            pixel_w,
            0.0,
            origin_y + row0 * pixel_h,
            0.0,
            pixel_h,
        )
        window = Dataset.create_from_array(
            arr=array,
            geo=geo,
            epsg=dataset.epsg,
            no_data_value=_source_no_data(dataset),
        )
        window.to_file(str(out_path))
    finally:
        # Drop the /vsicurl handle — an open remote dataset can hang the
        # interpreter at exit on GDAL's curl-handle cleanup (A1b).
        dataset = None
    return out_path


def window_crop(
    url: str, bbox: list[float], out_path: Path, *, epsg: int = 4326
) -> Path:
    """Windowed `/vsicurl` read of a remote COG (the Global Wind Atlas path).

    Args:
        url: The figshare COG download URL for the layer.
        bbox: `[west, south, east, north]` window in `epsg`.
        out_path: Destination GeoTIFF path.
        epsg: CRS of `bbox`. Defaults to `4326`.

    Returns:
        Path: `out_path`, the written window GeoTIFF.
    """
    return read_part_to_geotiff(vsicurl(url), bbox, out_path, epsg=epsg)


def zip_cache_path(url: str, cache_dir: Path) -> Path:
    """Return the on-disk cache path a ZIP URL downloads to.

    Args:
        url: The Global Solar Atlas `*.zip` download URL.
        cache_dir: Directory the ZIP is cached in.

    Returns:
        Path: `cache_dir / <url filename>`.
    """
    name = Path(urlsplit(url).path).name or "download.zip"
    return cache_dir / name


def download_zip(url: str, cache_dir: Path, *, timeout: float = 600.0) -> Path:
    """Download a ZIP archive into the cache once, reusing it if present.

    A present, non-empty cached file is returned without a network call, so the
    multi-GB Global Solar Atlas archives are fetched at most once.

    Args:
        url: The `*.zip` download URL.
        cache_dir: Directory to stream the archive into (created if absent).
        timeout: Per-request HTTP timeout in seconds. Defaults to `600`.

    Returns:
        Path: The cached ZIP path.

    Raises:
        requests.HTTPError: If the download responds with an error status.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = zip_cache_path(url, cache_dir)
    if target.exists() and target.stat().st_size > 0:
        return target
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    if chunk:
                        handle.write(chunk)
    except BaseException:
        # Don't leave a truncated .part behind for a failed / interrupted fetch.
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


def inner_tif(zip_path: Path) -> str:
    """Return the GeoTIFF member name inside a downloaded ZIP archive.

    Args:
        zip_path: A cached `*.zip` archive.

    Returns:
        str: The first `.tif` / `.tiff` member's name.

    Raises:
        ValueError: If the archive holds no GeoTIFF member.
    """
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if name.lower().endswith((".tif", ".tiff")):
                return name
    raise ValueError(f"{zip_path} contains no GeoTIFF (.tif) member.")


def download_cache_crop(
    url: str,
    bbox: list[float],
    out_path: Path,
    cache_dir: Path,
    *,
    epsg: int = 4326,
    timeout: float = 600.0,
) -> Path:
    """Download a ZIP once, then windowed-read the bbox from its member.

    The Global Solar Atlas path: the deflate ZIP cannot be range-windowed over
    the network, so it is fetched once into `cache_dir` and the bbox window is
    read from the local `/vsizip/<zip>/<member.tif>` (the `ghsl`
    download-then-localise model).

    Args:
        url: The Global Solar Atlas `*.zip` download URL.
        bbox: `[west, south, east, north]` window in `epsg`.
        out_path: Destination GeoTIFF path.
        cache_dir: Directory the ZIP is cached in.
        epsg: CRS of `bbox`. Defaults to `4326`.
        timeout: Per-request HTTP timeout for the download. Defaults to `600`.

    Returns:
        Path: `out_path`, the written window GeoTIFF.
    """
    zip_path = download_zip(url, cache_dir, timeout=timeout)
    member = inner_tif(zip_path)
    return read_part_to_geotiff(
        f"/vsizip/{zip_path}/{member}", bbox, out_path, epsg=epsg
    )
