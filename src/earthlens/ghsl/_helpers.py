"""URL builder, Mollweide tile-grid index, and download/unzip helpers for GHSL.

The JRC serves every GHSL artefact over a **deterministic, documented file
tree** (verified live 2026-05) — no SDK, no auth. This module owns the
provider glue earthlens is responsible for: building the exact `.zip` URL for
a `(product, epoch, release, resolution)` (and per-tile suffix), intersecting
an AOI with the fixed 18×36 Mollweide land tile grid to pick the tiles to
fetch, and streaming + unzipping the result to the `.tif` inside.

The URL convention (verified byte-for-byte):

```
{BASE}/{FAMILY}_GLOBE_R{REL}/{STEM}/V{maj}-{min}/[tiles/]{STEM}_V{maj}_{min}[_R{r}_C{c}].zip
```

where `FAMILY` is the product-family directory token (e.g. `GHS_BUILT_H`),
`STEM` is `{code}_E{year}_GLOBE_R{REL}_{crs}_{res}` (the file-stem token
`code` carries any sub-product suffix, e.g. `GHS_BUILT_H_ANBH`), `V{maj}-{min}`
appears in the path but `V{maj}_{min}` in the filename, and the `_R{r}_C{c}`
suffix + `tiles/` directory are present only for the per-tile artefacts.
Everything is `.zip`; each zip contains the `.tif` (same stem) plus sidecars.
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from earthlens.base.archive import extract_members

from earthlens.base.http import HttpClient
from earthlens.ghsl.catalog import RES_TO_TOKEN, native_source_crs
from earthlens.base.http import RequestsGet as _RequestsGet

#: Root of the JRC open-data GHSL file tree (anonymous HTTPS, no auth).
BASE_URL: str = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL"

#: Path to the bundled 18×36 Mollweide land tile schema (375 tiles, ESRI:54009).
TILE_SCHEMA_PATH: Path = Path(__file__).parent / "tile_schema.geojson"

#: Points sampled per WGS84 bbox edge before transforming to Mollweide, so the
#: curved Mollweide image of the (straight) lon/lat box is captured rather than
#: clipping its bowed edges to the 4 transformed corners.
_DENSIFY_PER_EDGE: int = 16


def _ghsl_stem(
    code: str, epoch: int, release: str, crs: str, res_token: str, region: str
) -> str:
    """Build the GHSL file-stem token for one artefact.

    Args:
        code: Product file-stem token (`"GHS_POP"`, `"GHS_BUILT_H_ANBH"`).
        epoch: Reference year.
        release: Release id (`"R2023A"`).
        crs: Source CRS token (`"54009"` / `"4326"`).
        res_token: JRC resolution token (`"100"`, `"1000"`, `"10"`, `"3ss"`,
            `"30ss"`).
        region: Region token (`"GLOBE"`, `"EUROPE"`, `"ARCTIC"`).

    Returns:
        str: e.g. `"GHS_POP_E2020_GLOBE_R2023A_54009_100"`.
    """
    return f"{code}_E{epoch}_{region}_{release}_{crs}_{res_token}"


def ghsl_url(
    family: str,
    code: str,
    epoch: int,
    release: str,
    resolution: str,
    *,
    tile: str | None = None,
    version: tuple[str, str] = ("1", "0"),
    region: str = "GLOBE",
    nested: bool = False,
) -> str:
    """Build the deterministic JRC `.zip` URL for one artefact.

    Args:
        family: Product-family directory token (`"GHS_POP"`,
            `"GHS_BUILT_H"`).
        code: Product file-stem token (equals `family` except for the
            `AGBH`/`ANBH`, `FUN`/`MSZ`, `NRES`, `VEG` sub-products).
        epoch: Reference year.
        release: Release id (`"R2023A"`).
        resolution: Friendly resolution label (`"100m"`); its source CRS is
            derived via `native_source_crs`.
        tile: Optional `R{r}_C{c}` tile id; `None` builds the whole-globe URL.
        version: `(major, minor)` data version. `V{maj}-{min}` in the path,
            `V{maj}_{min}` in the filename.
        region: Region token in the path (`"GLOBE"` default, `"EUROPE"`,
            `"ARCTIC"`).
        nested: When `True`, the per-epoch directory sits under an
            intermediate `{code}_{region}_{release}/` sub-product directory
            (the R2022A layout).

    Returns:
        str: The fully-qualified `.zip` URL.

    Raises:
        ValueError: If `resolution` is not a known GHSL resolution.

    Examples:
        - The verified R2023A whole-globe + per-tile URLs:
            ```python
            >>> from earthlens.ghsl._helpers import ghsl_url
            >>> ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "100m",
            ...          tile="R6_C18").split("/ftp/")[1]
            'jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_54009_100/V1-0/tiles/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C18.zip'
            >>> ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "1km").split("/V1-0/")[1]
            'GHS_POP_E2020_GLOBE_R2023A_54009_1000_V1_0.zip'

            ```
        - The nested R2022A layout inserts the sub-product directory:
            ```python
            >>> from earthlens.ghsl._helpers import ghsl_url
            >>> ghsl_url("GHS_BUILT_S", "GHS_BUILT_S_NRES", 2020, "R2022A",
            ...          "1km", nested=True).split("/ftp/")[1]
            'jrc-opendata/GHSL/GHS_BUILT_S_GLOBE_R2022A/GHS_BUILT_S_NRES_GLOBE_R2022A/GHS_BUILT_S_NRES_E2020_GLOBE_R2022A_54009_1000/V1-0/GHS_BUILT_S_NRES_E2020_GLOBE_R2022A_54009_1000_V1_0.zip'

            ```
    """
    if resolution not in RES_TO_TOKEN:
        raise ValueError(
            f"unknown GHSL resolution {resolution!r}; known: {sorted(RES_TO_TOKEN)}."
        )
    crs = native_source_crs(resolution)
    res_token = RES_TO_TOKEN[resolution]
    maj, minr = version
    stem = _ghsl_stem(code, epoch, release, crs, res_token, region)
    fam_dir = f"{family}_{region}_{release}"
    middle = f"{code}_{region}_{release}/" if nested else ""
    suffix = f"_{tile}" if tile else ""
    fname = f"{stem}_V{maj}_{minr}{suffix}.zip"
    sub = "tiles/" if tile else ""
    return f"{BASE_URL}/{fam_dir}/{middle}{stem}/V{maj}-{minr}/{sub}{fname}"


@lru_cache(maxsize=1)
def _load_tile_schema() -> tuple[tuple[str, float, float, float, float], ...]:
    """Load the bundled Mollweide tile grid as `(tile_id, l, b, r, t)` tuples.

    Returns:
        tuple: One `(tile_id, left, bottom, right, top)` per land tile, in
            Mollweide (ESRI:54009) metres. Cached after first read.
    """
    with open(TILE_SCHEMA_PATH, encoding="utf-8") as stream:
        gj = json.load(stream)
    rows: list[tuple[str, float, float, float, float]] = []
    for feat in gj["features"]:
        p = feat["properties"]
        rows.append(
            (
                str(p["tile_id"]),
                float(p["left"]),
                float(p["bottom"]),
                float(p["right"]),
                float(p["top"]),
            )
        )
    return tuple(rows)


def _bbox_to_mollweide_envelope(
    bbox_wgs84: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Transform a WGS84 bbox to its Mollweide axis-aligned envelope.

    Densifies the (straight) lon/lat box edges before transforming so the
    bowed Mollweide image is captured, then returns the bounding rectangle of
    the transformed points — a conservative superset suitable for tile
    selection.

    Args:
        bbox_wgs84: `(west, south, east, north)` in degrees.

    Returns:
        tuple[float, float, float, float]: `(left, bottom, right, top)` in
            Mollweide (ESRI:54009) metres.
    """
    from pyproj import Transformer

    west, south, east, north = bbox_wgs84
    transformer = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    n = _DENSIFY_PER_EDGE
    lons: list[float] = []
    lats: list[float] = []
    for i in range(n + 1):
        frac = i / n
        # bottom + top edges (lon varies)
        lon = west + (east - west) * frac
        lons.extend([lon, lon])
        lats.extend([south, north])
        # left + right edges (lat varies)
        lat = south + (north - south) * frac
        lons.extend([west, east])
        lats.extend([lat, lat])
    xs, ys = transformer.transform(lons, lats)
    return min(xs), min(ys), max(xs), max(ys)


def tiles_for_bbox(bbox_wgs84: tuple[float, float, float, float]) -> list[str]:
    """Return the land tile ids whose extent intersects the AOI.

    Args:
        bbox_wgs84: `(west, south, east, north)` in degrees.

    Returns:
        list[str]: Sorted `R{r}_C{c}` tile ids intersecting the AOI's
            Mollweide envelope. Empty when the AOI covers only ocean / falls
            outside every land tile.

    Examples:
        - A small Moroccan-coast AOI selects the verified tile:
            ```python
            >>> from earthlens.ghsl._helpers import tiles_for_bbox
            >>> tiles_for_bbox((-9.0, 30.5, -8.5, 31.0))
            ['R6_C18']

            ```
        - An open-ocean AOI (South Pacific) selects nothing:
            ```python
            >>> from earthlens.ghsl._helpers import tiles_for_bbox
            >>> tiles_for_bbox((-140.0, -40.0, -139.8, -39.8))
            []

            ```
    """
    left, bottom, right, top = _bbox_to_mollweide_envelope(bbox_wgs84)
    hits = [
        tile_id
        for tile_id, l, b, r, t in _load_tile_schema()
        if not (r <= left or l >= right or t <= bottom or b >= top)
    ]
    return sorted(hits, key=_tile_sort_key)


def _tile_sort_key(tile_id: str) -> tuple[int, int]:
    """Sort key turning `"R6_C18"` into `(6, 18)` for natural ordering."""
    row, col = tile_id.split("_")
    return int(row[1:]), int(col[1:])


def download_and_unzip(
    url: str,
    dest_dir: Path,
    *,
    session: requests.Session | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 120.0,
    chunk_size: int = 1 << 20,
) -> Path:
    """Stream a GHSL `.zip` to `dest_dir`, unzip it, return the `.tif` inside.

    Idempotent: if the expected `.tif` already exists it is returned without
    re-downloading. Retries transient HTTP failures with exponential backoff.

    Args:
        url: A GHSL `.zip` URL (from `ghsl_url`).
        dest_dir: Directory to download + extract into (created if absent).
        session: Optional shared `requests.Session` for connection reuse.
        retries: Number of attempts before giving up.
        backoff: Base seconds for exponential backoff between retries.
        timeout: Per-request timeout in seconds.
        chunk_size: Streaming chunk size in bytes.

    Returns:
        pathlib.Path: The extracted `.tif` (the single GeoTIFF member; JRC
            zips also carry a PDF + xlsx sidecar which are ignored).

    Raises:
        ValueError: If the zip contains no `.tif` member.
        requests.HTTPError: If every attempt fails (the last error is raised).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_name = url.rsplit("/", 1)[-1]
    tif_name = zip_name[: -len(".zip")] + ".tif"
    tif_path = dest_dir / tif_name
    if tif_path.exists():
        return tif_path

    zip_path = dest_dir / zip_name
    _download(url, zip_path, session, retries, backoff, timeout, chunk_size)
    (extracted,) = extract_members(zip_path, dest_dir, include=(".tif",), single=True)
    if extracted != tif_path:
        extracted.replace(tif_path)
    zip_path.unlink(missing_ok=True)
    return tif_path


def _assert_safe_members(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Reject archive members that would extract outside `dest_dir` (Zip Slip).

    The JRC tree is a trusted source, but extracting attacker-controlled member
    names (CWE-22) is the standard untrusted-archive pitfall, so every member's
    resolved destination is checked to stay within `dest_dir` before any
    extraction runs.

    Args:
        zf: An open `zipfile.ZipFile`.
        dest_dir: The directory members will be extracted into.

    Raises:
        ValueError: If any member resolves outside `dest_dir`.
    """
    base = dest_dir.resolve()
    for name in zf.namelist():
        target = (dest_dir / name).resolve()
        if target != base and base not in target.parents:
            raise ValueError(
                f"refusing to extract unsafe path {name!r} from the archive "
                f"(escapes {dest_dir})."
            )


#: Matches a GHSL data-version directory name (`V1-0`, `V2-0`, `V1-1`, …).
_VERSION_RE = re.compile(r"^V(\d+)-(\d+)$")
#: Matches an Apache-autoindex `href="…"` entry.
_HREF_RE = re.compile(r'href="([^"?][^"]*)"')


def list_remote_dir(
    url: str, *, session: requests.Session | None = None, timeout: float = 60.0
) -> list[str]:
    """List the entry names in a JRC Apache-autoindex directory.

    Args:
        url: Directory URL (with or without a trailing slash).
        session: Optional shared `requests.Session`.
        timeout: Request timeout in seconds.

    Returns:
        list[str]: The `href` entry names (sub-directories keep their trailing
            slash), excluding the parent-directory and column-sort links.
    """
    get = session.get if session is not None else requests.get
    resp = get(url if url.endswith("/") else url + "/", timeout=timeout)
    resp.raise_for_status()
    names = []
    for href in _HREF_RE.findall(resp.text):
        if href in ("/", "..", "../") or href.startswith("/"):
            continue
        names.append(href)
    return names


def latest_version_dir(
    family_url: str, *, session: requests.Session | None = None
) -> str:
    """Return the highest `V{maj}-{min}` directory name under a family URL.

    Args:
        family_url: A product-family directory URL (e.g.
            `…/GHS_DUC_GLOBE_R2023A`).
        session: Optional shared `requests.Session`.

    Returns:
        str: The newest version directory name (e.g. `"V2-0"`).

    Raises:
        ValueError: If no `V{maj}-{min}` directory is found.
    """
    versions: list[tuple[int, int, str]] = []
    for name in list_remote_dir(family_url, session=session):
        match = _VERSION_RE.match(name.rstrip("/"))
        if match:
            versions.append(
                (int(match.group(1)), int(match.group(2)), name.rstrip("/"))
            )
    if not versions:
        raise ValueError(f"no V{{maj}}-{{min}} version directory under {family_url}.")
    versions.sort()
    return versions[-1][2]


def download_and_extract(
    url: str,
    dest_dir: Path,
    *,
    session: requests.Session | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 120.0,
    chunk_size: int = 1 << 20,
) -> list[Path]:
    """Stream a `.zip` to `dest_dir` and extract **all** members (tabular path).

    Unlike `download_and_unzip` (which selects the single `.tif`), this keeps
    every member — used for the tabular DUC / WUP-statistics products whose
    payload is a CSV / GeoPackage / xlsx, not a raster.

    Args:
        url: A `.zip` URL.
        dest_dir: Directory to download + extract into (created if absent).
        session: Optional shared `requests.Session`.
        retries: Attempts before giving up.
        backoff: Base backoff seconds.
        timeout: Per-request timeout.
        chunk_size: Streaming chunk size.

    Returns:
        list[Path]: The extracted member paths (the `.zip` itself is removed).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / url.rsplit("/", 1)[-1]
    _download(url, zip_path, session, retries, backoff, timeout, chunk_size)
    with zipfile.ZipFile(zip_path) as zf:
        _assert_safe_members(zf, dest_dir)
        members = [m for m in zf.namelist() if not m.endswith("/")]
        zf.extractall(dest_dir)
    zip_path.unlink(missing_ok=True)
    return [dest_dir / m for m in members]
def _download(
    url: str,
    zip_path: Path,
    session: requests.Session | None,
    retries: int,
    backoff: float,
    timeout: float,
    chunk_size: int,
) -> None:
    """Stream `url` to `zip_path` with retry + exponential backoff.

    Delegates the transfer to `HttpClient.download`, which streams to a sibling
    `<zip_path>.part` and renames it on success (removing the temp on any
    failure) — so an interrupted download never leaves a truncated archive at
    `zip_path`. The retry policy reproduces the historical loop: an error status
    (`429`/`5xx`, via `raise_for_status`) or a transport `RequestException` /
    `OSError` retries the whole download, waiting `backoff * 2**attempt` between
    the `retries` attempts (bounded by the default back-off ceiling, which the
    exponential wait never reaches). When every attempt is exhausted the final
    error is wrapped in a `requests.HTTPError` carrying the same message the
    hand-rolled loop raised.

    Args:
        url: The `.zip` URL.
        zip_path: Local path to write.
        session: Optional shared session.
        retries: Attempts before giving up.
        backoff: Base backoff seconds.
        timeout: Per-request timeout.
        chunk_size: Streaming chunk size.

    Raises:
        requests.HTTPError: If every attempt fails (the last error is wrapped).
    """
    client = HttpClient(
        session=session if session is not None else _RequestsGet(),
        status_forcelist=(429, 500, 502, 503, 504),
        retry_on_exceptions=(requests.RequestException, OSError),
        raise_for_status=True,
        max_retries=max(retries - 1, 0),
        backoff_factor=backoff,
        sleep=time.sleep,
    )
    try:
        client.download(
            url, zip_path, chunk=chunk_size, progress=False, timeout=timeout
        )
    except (requests.RequestException, OSError) as exc:
        raise requests.HTTPError(
            f"GHSL: download {url} failed after {retries} attempts: {exc}"
        ) from exc
