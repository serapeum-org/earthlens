"""Bristol FABDEM V1-2 file-tree glue: bbox → 1° cells → 10° bundle zips.

FABDEM V1-2 is served from the University of Bristol data repository over a
deterministic, anonymous HTTPS tree (verified live 2026-08-09): each 10°×10°
bundle is one `.zip` at `{BASE_URL}/{bundle}_FABDEM_V1-2.zip`, and each bundle
holds one Cloud-Optimized GeoTIFF per 1°×1° land cell, named
`{cell}_FABDEM_V1-2.tif`. Ocean-only bundles and ocean-only cells are simply
absent upstream (a `404` for a bundle, a missing member inside one), so both
are skipped rather than treated as errors.

Cell and bundle ids use the **SW-corner** convention: latitude as `N`/`S` plus
two zero-padded digits, longitude as `E`/`W` plus three zero-padded digits
(e.g. `N50E000`, `S10W073`). A 10° bundle id is `{sw}-{ne}` — the SW corner of
its 10° block joined to the corner 10° north-east (e.g. `N50E000-N60E010`).
"""

from __future__ import annotations

import math
import shutil
import time
import zipfile
from pathlib import Path

import requests

from earthlens.base.http import HttpClient, thread_local_session

#: Root of the Bristol FABDEM V1-2 file tree (anonymous HTTPS, no auth).
BASE_URL: str = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn"

#: The published data version; appears in every bundle and tile file name.
DATASET_VERSION: str = "V1-2"


def _corner(lat: int, lon: int) -> str:
    """Format an integer SW corner as a FABDEM corner token.

    Args:
        lat: Integer latitude of the SW corner in degrees (negative = south).
        lon: Integer longitude of the SW corner in degrees (negative = west).

    Returns:
        str: The token, e.g. `"N50E000"`, `"S10W073"` (2-digit latitude,
            3-digit longitude, zero-padded).
    """
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def tile_name(lat: int, lon: int) -> str:
    """Return the 1° GeoTIFF member name for the cell with SW corner (lat, lon).

    Args:
        lat: Integer SW-corner latitude of the 1° cell.
        lon: Integer SW-corner longitude of the 1° cell.

    Returns:
        str: e.g. `"N50E000_FABDEM_V1-2.tif"`.
    """
    return f"{_corner(lat, lon)}_FABDEM_{DATASET_VERSION}.tif"


def bundle_id(lat: int, lon: int) -> str:
    """Return the 10° bundle id containing the 1° cell at SW corner (lat, lon).

    Args:
        lat: Integer SW-corner latitude of the 1° cell.
        lon: Integer SW-corner longitude of the 1° cell.

    Returns:
        str: The `{sw}-{ne}` bundle id, e.g. `"N50E000-N60E010"`.
    """
    base_lat = math.floor(lat / 10) * 10
    base_lon = math.floor(lon / 10) * 10
    return f"{_corner(base_lat, base_lon)}-{_corner(base_lat + 10, base_lon + 10)}"


def bundle_url(bundle: str) -> str:
    """Return the deterministic Bristol `.zip` URL for a 10° bundle id.

    Args:
        bundle: A `{sw}-{ne}` bundle id from `bundle_id`.

    Returns:
        str: The fully-qualified `.zip` URL.

    Examples:
        - The verified bundle URL:
            ```python
            >>> from earthlens.fabdem._helpers import bundle_url
            >>> bundle_url("N50E000-N60E010")
            'https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/N50E000-N60E010_FABDEM_V1-2.zip'

            ```
    """
    return f"{BASE_URL}/{bundle}_FABDEM_{DATASET_VERSION}.zip"


def cells_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Return the SW corners of every 1° cell intersecting the AOI bbox.

    A 1° cell with SW corner `(lat, lon)` covers `[lat, lat+1] × [lon, lon+1]`;
    it is selected when that square overlaps the bbox with positive area (an
    edge-only touch does not count). Corners are clamped to the valid FABDEM
    grid (`lat ∈ [-90, 89]`, `lon ∈ [-180, 179]`). The bbox must be
    non-antimeridian (`west <= east`); the backend rejects a `west > east` AOI
    up front, since mosaicking the two far-apart seam columns would span the
    whole globe.

    Args:
        bbox: `(west, south, east, north)` in degrees, with `west <= east`.

    Returns:
        list[tuple[int, int]]: Sorted `(lat, lon)` SW corners.

    Examples:
        - A small AOI over the English Channel selects four cells:
            ```python
            >>> from earthlens.fabdem._helpers import cells_for_bbox
            >>> cells_for_bbox((0.4, 50.4, 1.6, 51.6))
            [(50, 0), (50, 1), (51, 0), (51, 1)]

            ```
    """
    west, south, east, north = bbox
    cells: list[tuple[int, int]] = []
    for lat in range(math.floor(south), math.ceil(north)):
        for lon in range(math.floor(west), math.ceil(east)):
            overlaps = lat + 1 > south and lat < north and lon + 1 > west and lon < east
            if overlaps and -90 <= lat <= 89 and -180 <= lon <= 179:
                cells.append((lat, lon))
    return sorted(cells)


def bundles_for_bbox(
    bbox: tuple[float, float, float, float],
) -> dict[str, list[str]]:
    """Map each needed 10° bundle id to the intersecting 1° tile member names.

    Args:
        bbox: `(west, south, east, north)` in degrees.

    Returns:
        dict[str, list[str]]: Bundle id → sorted tile file names, itself
            ordered by bundle id. Empty when the AOI covers no land cell.

    Examples:
        - An AOI straddling two 10° blocks needs two bundles:
            ```python
            >>> from earthlens.fabdem._helpers import bundles_for_bbox
            >>> plan = bundles_for_bbox((9.4, 50.4, 10.6, 50.6))
            >>> sorted(plan)
            ['N50E000-N60E010', 'N50E010-N60E020']
            >>> plan['N50E010-N60E020']
            ['N50E010_FABDEM_V1-2.tif']

            ```
    """
    groups: dict[str, list[str]] = {}
    for lat, lon in cells_for_bbox(bbox):
        groups.setdefault(bundle_id(lat, lon), []).append(tile_name(lat, lon))
    return {bundle: sorted(names) for bundle, names in sorted(groups.items())}


def download_bundle(
    url: str,
    dest_dir: Path,
    *,
    session: requests.Session | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 300.0,
    chunk_size: int = 1 << 20,
) -> Path | None:
    """Stream a FABDEM bundle `.zip` to `dest_dir`, retrying transient failures.

    Idempotent: an already-downloaded `.zip` is returned without re-fetching.
    A `404` means the 10° block is ocean-only (no bundle published), so `None`
    is returned rather than raising — the caller skips it. Other transient
    HTTP failures (`429`/`5xx`) are retried with exponential backoff.

    Args:
        url: A bundle `.zip` URL from `bundle_url`.
        dest_dir: Directory to download into (created if absent).
        session: Optional shared `requests.Session` for connection reuse.
        retries: Number of attempts before giving up.
        backoff: Base seconds for exponential backoff between retries.
        timeout: Per-request timeout in seconds (bundles are 0.8–2.4 GB).
        chunk_size: Streaming chunk size in bytes.

    Returns:
        pathlib.Path | None: The downloaded `.zip`, or `None` when the bundle
            does not exist upstream (`404` — an ocean-only block).

    Raises:
        requests.HTTPError: If a non-404 error persists after every attempt.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / url.rsplit("/", 1)[-1]
    if zip_path.exists():
        return zip_path
    # Retry only transport errors, not HTTP status errors: a 404 (the expected
    # ocean-only-bundle case) is a `requests.HTTPError` — a `RequestException`
    # subclass — so retrying that class would sleep + retry the 404 before the
    # caller converts it to `None`. 429/5xx are still retried via
    # `status_forcelist` (they retry before `raise_for_status`).
    client = HttpClient(
        session=session if session is not None else thread_local_session("fabdem"),
        status_forcelist=(429, 500, 502, 503, 504),
        retry_on_exceptions=(requests.ConnectionError, requests.Timeout, OSError),
        raise_for_status=True,
        max_retries=max(retries - 1, 0),
        backoff_factor=backoff,
        sleep=time.sleep,
    )
    try:
        # `expect_magic` rejects an HTTP-200 non-zip body (e.g. an HTML error
        # page) up front, so a corrupt file is never cached as a complete `.zip`.
        client.download(
            url,
            zip_path,
            chunk=chunk_size,
            progress=False,
            timeout=timeout,
            expect_magic=b"PK\x03\x04",
        )
    except requests.HTTPError as exc:
        response = exc.response
        if response is not None and response.status_code == 404:
            zip_path.unlink(missing_ok=True)
            return None
        raise
    # The transport errors the retry policy re-raises once exhausted — listed
    # explicitly (not the base `RequestException`) so `HTTPError` handled above is
    # not caught together with its base class.
    except (requests.ConnectionError, requests.Timeout, OSError) as exc:
        raise requests.HTTPError(
            f"FABDEM: download {url} failed after {retries} attempts: {exc}"
        ) from exc
    return zip_path


def extract_tiles(zip_path: Path, dest_dir: Path, names: list[str]) -> list[Path]:
    """Extract the named GeoTIFF members from a bundle zip, flattened by basename.

    Extracting just the intersecting 1° tiles avoids unpacking a whole 10°
    bundle (up to ~100 COGs) when a small AOI needs only a few. A requested
    name that is not in the archive is silently skipped — it is an ocean-only
    cell that was never published. Members are matched and written by
    **basename** directly under `dest_dir` (a flat cache the backend looks up by
    bare tile name); this also neutralises any path-traversal member name
    (`../evil.tif` lands as `evil.tif` inside `dest_dir`, never escaping it).

    Args:
        zip_path: A downloaded bundle `.zip`.
        dest_dir: Directory to extract into (created if absent).
        names: Tile file names to extract (from `tile_name`).

    Returns:
        list[Path]: The extracted `.tif` paths (`dest_dir/<basename>`), sorted;
            empty when none of `names` is present in the archive.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(names)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        # Map wanted basename -> archive member (handles a hypothetical
        # folder-nested archive; the Bristol bundles are flat today).
        by_basename = {Path(m).name: m for m in zf.namelist() if Path(m).name in wanted}
        for basename, member in sorted(by_basename.items()):
            target = dest_dir / basename
            if not target.exists():
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            out.append(target)
    return sorted(out)
