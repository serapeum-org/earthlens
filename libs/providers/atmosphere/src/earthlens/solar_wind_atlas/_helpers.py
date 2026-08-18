"""Stateless helpers for the Solar & Wind Atlas backend.

Two transports, one windowed-extraction core. The Global Wind Atlas layers are
range-accessible Cloud-Optimized GeoTIFFs on figshare, read **windowed** over
`/vsicurl/` so only the AOI bytes transfer (`window_crop`). The Global Solar
Atlas layers are DEFLATE-compressed ZIP archives with no random access, so they
are downloaded once into a cache and read **windowed from the local ZIP member**
(`download_cache_crop`). Both paths funnel through `read_part_to_geotiff`, which
delegates to `pyramids.dataset.Dataset.crop(bbox=)` — its windowed fast path
reads only the AOI's pixel window straight from the source (over `/vsicurl/` a
few hundred KB rather than the whole multi-GB raster) and never materialises the
whole global grid. `pyramids` applies the `/vsicurl` HTTP tuning (readdir/retry/
timeout, extensionless-URL handling) itself, so no GDAL env is set here.

All raster I/O goes through `pyramids`; the stdlib `zipfile` is used only to
name the GeoTIFF member inside a downloaded ZIP, never to read pixels.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests  # noqa: F401  # runtime seam so tests can monkeypatch this module's `requests`

from earthlens.base import close_quietly, ensure_no_data, widen_degenerate_bbox
from earthlens.base.http import HttpClient

#: No-data value stamped on a windowed crop when the source declares none.
_DEFAULT_NO_DATA = -9999.0

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

#: Stream chunk size (bytes) for the Global Solar Atlas ZIP download.
_DOWNLOAD_CHUNK = 1 << 20


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


def bbox_from_extent(space: SpatialExtent) -> list[float]:
    """Return the `[west, south, east, north]` bbox of a spatial extent.

    Args:
        space: A `SpatialExtent` (the backend's `self.space`) exposing
            `west` / `south` / `east` / `north`.

    Returns:
        list[float]: `[west, south, east, north]` in degrees (EPSG:4326).
    """
    return [space.west, space.south, space.east, space.north]


def read_part_to_geotiff(
    path: str, bbox: list[float], out_path: Path, *, epsg: int = 4326
) -> Path:
    """Read just the `bbox` window from a raster and write it as a GeoTIFF.

    Delegates the windowed read to `pyramids.dataset.Dataset.crop(bbox=)`, whose
    fast path resolves the bbox to a pixel window and reads **only** that window
    straight from the source: for a COG over `/vsicurl/` a few hundred KB rather
    than the whole multi-GB file, and for a `/vsizip/` member only that member's
    window. The source grid, CRS and no-data value are carried onto the crop, so
    genuinely-empty cells stay flagged. `pyramids` applies the `/vsicurl` HTTP
    tuning (readdir/retry/timeout, extensionless-URL handling) itself.

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

    dataset = Dataset.read_file(path)
    try:
        # A point / cell-edge AOI is widened to one source pixel so crop(bbox=)
        # yields a 1x1 window instead of raising on the zero-width box.
        geo = dataset.geotransform
        window = dataset.crop(
            bbox=widen_degenerate_bbox(bbox, geo[1], geo[5]), epsg=epsg
        )
    finally:
        # Drop the /vsicurl handle — an open remote dataset can hang the
        # interpreter at exit on GDAL's curl-handle cleanup (A1b).
        close_quietly(dataset)
    # crop carries the source's own no-data through; fall back to the default so
    # genuinely-empty cells stay flagged when the source declares none.
    window = ensure_no_data(window, _DEFAULT_NO_DATA)
    try:
        window.to_file(str(out_path))
    finally:
        close_quietly(window)
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
    multi-GB Global Solar Atlas archives are fetched at most once. The transfer
    is delegated to `HttpClient.download`, which streams to a sibling `.part`
    file and renames it on success (removing the temp on any failure), so an
    interrupted download never leaves a truncated archive. An error status
    raises immediately — these single-file archives are not retried.

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
    client = HttpClient(
        status_forcelist=(),
        max_backoff=None,
    )
    return client.download(
        url, target, chunk=_DOWNLOAD_CHUNK, progress=False, timeout=timeout
    )


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
