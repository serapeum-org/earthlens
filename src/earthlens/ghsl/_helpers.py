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
import time
import zipfile
from functools import lru_cache
from pathlib import Path

import requests
from loguru import logger

from earthlens.ghsl.catalog import RES_TO_TOKEN, native_source_crs

#: Root of the JRC open-data GHSL file tree (anonymous HTTPS, no auth).
BASE_URL: str = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL"

#: Path to the bundled 18×36 Mollweide land tile schema (375 tiles, ESRI:54009).
TILE_SCHEMA_PATH: Path = Path(__file__).parent / "tile_schema.geojson"

#: Points sampled per WGS84 bbox edge before transforming to Mollweide, so the
#: curved Mollweide image of the (straight) lon/lat box is captured rather than
#: clipping its bowed edges to the 4 transformed corners.
_DENSIFY_PER_EDGE: int = 16


def _ghsl_stem(code: str, epoch: int, release: str, crs: str, res_token: str) -> str:
    """Build the GHSL file-stem token for one artefact.

    Args:
        code: Product file-stem token (`"GHS_POP"`, `"GHS_BUILT_H_ANBH"`).
        epoch: Reference year.
        release: Release id (`"R2023A"`).
        crs: Source CRS token (`"54009"` / `"4326"`).
        res_token: JRC resolution token (`"100"`, `"1000"`, `"10"`, `"3ss"`,
            `"30ss"`).

    Returns:
        str: e.g. `"GHS_POP_E2020_GLOBE_R2023A_54009_100"`.
    """
    return f"{code}_E{epoch}_GLOBE_{release}_{crs}_{res_token}"


def ghsl_url(
    family: str,
    code: str,
    epoch: int,
    release: str,
    resolution: str,
    *,
    tile: str | None = None,
    version: tuple[str, str] = ("1", "0"),
) -> str:
    """Build the deterministic JRC `.zip` URL for one artefact.

    Args:
        family: Product-family directory token (`"GHS_POP"`,
            `"GHS_BUILT_H"`).
        code: Product file-stem token (equals `family` except for the
            `AGBH`/`ANBH`, `FUN`/`MSZ`, `NRES` sub-products).
        epoch: Reference year.
        release: Release id (`"R2023A"`).
        resolution: Friendly resolution label (`"100m"`); its source CRS is
            derived via `native_source_crs`.
        tile: Optional `R{r}_C{c}` tile id; `None` builds the whole-globe URL.
        version: `(major, minor)` data version. `V{maj}-{min}` in the path,
            `V{maj}_{min}` in the filename.

    Returns:
        str: The fully-qualified `.zip` URL.

    Raises:
        ValueError: If `resolution` is not a known GHSL resolution.

    Examples:
        - The verified whole-globe and per-tile URLs:
            ```python
            >>> from earthlens.ghsl._helpers import ghsl_url
            >>> ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "100m",
            ...          tile="R6_C18").split("/ftp/")[1]
            'jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_54009_100/V1-0/tiles/GHS_POP_E2020_GLOBE_R2023A_54009_100_V1_0_R6_C18.zip'
            >>> ghsl_url("GHS_POP", "GHS_POP", 2020, "R2023A", "1km").split("/V1-0/")[1]
            'GHS_POP_E2020_GLOBE_R2023A_54009_1000_V1_0.zip'

            ```
    """
    if resolution not in RES_TO_TOKEN:
        raise ValueError(
            f"unknown GHSL resolution {resolution!r}; known: {sorted(RES_TO_TOKEN)}."
        )
    crs = native_source_crs(resolution)
    res_token = RES_TO_TOKEN[resolution]
    maj, minr = version
    stem = _ghsl_stem(code, epoch, release, crs, res_token)
    fam_dir = f"{family}_GLOBE_{release}"
    suffix = f"_{tile}" if tile else ""
    fname = f"{stem}_V{maj}_{minr}{suffix}.zip"
    sub = "tiles/" if tile else ""
    return f"{BASE_URL}/{fam_dir}/{stem}/V{maj}-{minr}/{sub}{fname}"


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
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".tif")]
        if not members:
            raise ValueError(
                f"GHSL zip {url} contains no .tif member (found {zf.namelist()})."
            )
        member = members[0]
        zf.extract(member, dest_dir)
    extracted = dest_dir / member
    if extracted != tif_path:
        extracted.replace(tif_path)
    zip_path.unlink(missing_ok=True)
    return tif_path


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

    Args:
        url: The `.zip` URL.
        zip_path: Local path to write.
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
                with open(zip_path, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        handle.write(chunk)
            return
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            zip_path.unlink(missing_ok=True)
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"GHSL: download {url} failed (attempt {attempt}/{retries}): "
                    f"{type(exc).__name__}: {exc}; retrying in {wait:.0f}s."
                )
                time.sleep(wait)
    raise requests.HTTPError(
        f"GHSL: download {url} failed after {retries} attempts: {last_exc}"
    )
