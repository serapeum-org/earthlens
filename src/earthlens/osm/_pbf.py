"""Fetch + read helpers for the OpenStreetMap `pbf` protocol (`G9`..`G15`).

Two concerns are factored here so `osm/backend.py` only routes:

* `download_extract` — fetch a Geofabrik regional `.osm.pbf` extract over
  anonymous HTTPS (`https://download.geofabrik.de/<path>-latest.osm.pbf`) and
  **cache it to disk** (`G13`): a repeat request for the same region reuses the
  cached file. The download is atomic (the shared
  :class:`~earthlens.base.http.HttpClient` writes a sibling `.part` and renames
  on success) and, by default, integrity-checked against Geofabrik's
  `.osm.pbf.md5` sidecar. A multi-GB extract logs a large-file warning before
  it is fetched.
* `read_pbf` — read one layer (buildings / roads / pois / …) from a local
  `.osm.pbf` into a pyramids `~pyramids.feature.collection.FeatureCollection`
  (`G14`), wrapping the **OSM-domain SDK** (`G9`): `pyrosm` for the regional
  in-memory path (`engine="pyrosm"`, the default) or `pyosmium`/`osmium` for the
  bounded-memory streaming path (`engine="pyosmium"`, for planet-scale or when
  `pyrosm` cannot hold the file). Both SDKs are imported **lazily** with an
  install hint, so `earthlens` (and the `overpass`/`ohsome` protocols) import
  without `earthlens[osm-pbf]`.

No `xarray` is imported anywhere here (`G7`); the result is assembled as a plain
`GeoDataFrame` and handed to pyramids via `to_fc`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from loguru import logger

from earthlens.base.http import HttpClient
from earthlens.osm._helpers import OSM_CRS, empty_fc, to_fc

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

#: Root of Geofabrik's regional extract server. The full URL for a region is
#: `f"{GEOFABRIK_BASE_URL}/{path}-latest.osm.pbf"` (path e.g. `europe/malta`).
GEOFABRIK_BASE_URL = "https://download.geofabrik.de"

#: The two read engines a `pbf` request can route to.
Engine = Literal["pyrosm", "pyosmium"]

#: Extract size (bytes) above which `download_extract` logs a large-file
#: warning — a multi-GB fetch is slow and disk-heavy (`G13`). 1 GiB.
LARGE_FILE_WARN_BYTES = 1 * 1024**3

#: Extract size (bytes) above which the in-memory `pyrosm` engine is refused
#: (`G13`): `pyrosm` loads the whole file into memory, so a huge extract (a
#: continent, the planet) must use the streaming `pyosmium` engine instead.
#: 4 GiB.
MAX_PYROSM_BYTES = 4 * 1024**3

#: Per-layer read plan for the streaming `pyosmium` engine: the `pyrosm` reader
#: method → (primary OSM tag key, geometry strategy). The strategies are
#: `"point"` (tagged nodes → `Point`), `"line"` (tagged ways → `LineString`),
#: and `"area"` (tagged areas → `Polygon` / `MultiPolygon`). Unlike `pyrosm`
#: (which returns mixed geometry per layer), the `pyosmium` fallback yields the
#: layer's **primary** geometry kind under a single representative tag key.
_PYOSMIUM_LAYERS: dict[str, tuple[str, str]] = {
    "get_buildings": ("building", "area"),
    "get_network": ("highway", "line"),
    "get_pois": ("amenity", "point"),
    "get_landuse": ("landuse", "area"),
    "get_natural": ("natural", "area"),
    "get_boundaries": ("boundary", "area"),
}


def geofabrik_url(region_path: str) -> str:
    """Build the Geofabrik extract URL for a region path.

    Args:
        region_path: The Geofabrik path segment, e.g. `"europe/malta"`.

    Returns:
        str: The full `.osm.pbf` URL, e.g.
            `"https://download.geofabrik.de/europe/malta-latest.osm.pbf"`.

    Examples:
        - The `-latest.osm.pbf` suffix is appended to the path:
            ```python
            >>> from earthlens.osm._pbf import geofabrik_url
            >>> geofabrik_url("europe/malta")
            'https://download.geofabrik.de/europe/malta-latest.osm.pbf'

            ```
    """
    return f"{GEOFABRIK_BASE_URL}/{region_path}-latest.osm.pbf"


def _cache_name(region_path: str) -> str:
    """Return the flat cache filename for a region path (slashes → underscores).

    Args:
        region_path: The Geofabrik path segment, e.g. `"europe/malta"`.

    Returns:
        str: The cache basename, e.g. `"europe_malta-latest.osm.pbf"` — flat so
            two regions sharing a leaf name (`us/georgia`, `asia/georgia`) never
            collide.
    """
    return f"{region_path.replace('/', '_')}-latest.osm.pbf"


def _md5_of(path: Path) -> str:
    """Return the hex MD5 digest of a file, read in 1 MiB blocks.

    Args:
        path: The file to digest.

    Returns:
        str: The lower-case hex MD5 digest.
    """
    digest = hashlib.md5()  # noqa: S324 - integrity check vs Geofabrik sidecar, not security
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_md5(url: str, http: HttpClient) -> str | None:
    """Fetch and parse a Geofabrik `.osm.pbf.md5` sidecar.

    The sidecar body is `"<hash>  <basename>"`, so the first whitespace token
    is the digest. A network / HTTP failure returns `None` (integrity check is
    skipped rather than failing the whole fetch on a flaky sidecar).

    Args:
        url: The `.osm.pbf` URL (the `.md5` suffix is appended).
        http: The client used to GET the sidecar.

    Returns:
        str | None: The expected hex digest, or `None` when the sidecar could
            not be read or was empty.
    """
    try:
        body = http.get(f"{url}.md5").text
    except Exception as exc:  # noqa: BLE001 - a flaky sidecar must not fail the fetch
        logger.warning(f"Could not read the Geofabrik md5 sidecar ({exc}); skipping check")
        return None
    token = body.split()
    return token[0] if token else None


def download_extract(
    region_path: str,
    cache_dir: Path | str,
    *,
    http: HttpClient | None = None,
    progress: bool = True,
    verify_md5: bool = True,
) -> Path:
    """Fetch (and cache) a Geofabrik `.osm.pbf` extract for a region (`G13`).

    The extract is written under `cache_dir` as
    `<continent>_<region>-latest.osm.pbf`. A cached file is reused: when
    `verify_md5` is set the cached copy is validated against the current
    Geofabrik `.osm.pbf.md5` sidecar and re-downloaded on a mismatch; otherwise
    its mere presence is a cache hit. A fresh download streams atomically (via
    :class:`~earthlens.base.http.HttpClient`, which follows the Geofabrik `302`
    redirect and writes a `.part` renamed on success) and, when `verify_md5` is
    set, is checked against the sidecar — a mismatch removes the file and
    raises. An extract larger than `LARGE_FILE_WARN_BYTES` logs a warning before
    the fetch.

    Args:
        region_path: The Geofabrik path segment, e.g. `"europe/malta"`.
        cache_dir: Directory the extract is cached in (created if absent).
        http: HTTP client to use. Defaults to a fresh
            :class:`~earthlens.base.http.HttpClient` (retry/back-off on
            `5xx`/`429`).
        progress: Show a `tqdm` download progress bar.
        verify_md5: Validate the file against Geofabrik's `.osm.pbf.md5`
            sidecar (cache hit *and* fresh download). `False` skips the check.

    Returns:
        Path: The path to the cached `.osm.pbf` file.

    Raises:
        ValueError: If a freshly-downloaded file's MD5 does not match the
            sidecar.
        requests.HTTPError: If the download fails with a non-retryable status.
    """
    http = http if http is not None else HttpClient()
    url = geofabrik_url(region_path)
    cache_dir = Path(cache_dir)
    dest = cache_dir / _cache_name(region_path)

    if dest.exists():
        if not verify_md5:
            logger.info(f"OSM PBF cache hit: {dest}")
            return dest
        expected = _expected_md5(url, http)
        if expected is None or _md5_of(dest) == expected:
            logger.info(f"OSM PBF cache hit (md5 ok): {dest}")
            return dest
        logger.warning(f"Cached {dest.name} failed md5 check; re-downloading")

    _warn_large_extract(url, http)
    logger.info(f"Downloading Geofabrik extract {url}")
    http.download(url, dest, progress=progress, atomic=True)

    if verify_md5:
        expected = _expected_md5(url, http)
        if expected is not None and _md5_of(dest) != expected:
            dest.unlink(missing_ok=True)
            raise ValueError(
                f"MD5 mismatch for {url}: the downloaded file does not match the "
                "Geofabrik .osm.pbf.md5 sidecar (removed). Retry the download, or "
                "pass verify_md5=False to skip the check."
            )
    return dest


def _warn_large_extract(url: str, http: HttpClient) -> None:
    """Log a warning when the extract's `Content-Length` exceeds the threshold.

    Issues a `HEAD` to read `Content-Length`; a missing / unreadable size is
    silently ignored (the download proceeds without a size warning).

    Args:
        url: The `.osm.pbf` URL to size.
        http: The client used for the `HEAD`.
    """
    try:
        head = http.request("HEAD", url)
        size = int(head.headers.get("Content-Length", 0))
    except Exception:  # noqa: BLE001 - a failed HEAD must not block the download
        return
    if size >= LARGE_FILE_WARN_BYTES:
        logger.warning(
            f"Geofabrik extract is large (~{size / 1024**3:.1f} GB): {url}. The "
            "download is slow and disk-heavy; for a continent- or planet-scale "
            "file use engine='pyosmium' (streaming) rather than the default "
            "in-memory 'pyrosm' engine."
        )


def read_pbf(
    path: Path | str,
    *,
    pyrosm_method: str,
    network_type: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    engine: Engine = "pyrosm",
) -> FeatureCollection:
    """Read one layer from a local `.osm.pbf` into a `FeatureCollection` (`G14`).

    Routes on `engine`: `"pyrosm"` (default) reads the whole extract in memory
    and calls the named `pyrosm.OSM` method; `"pyosmium"` streams the file with
    bounded memory (for planet-scale, or a file too large for `pyrosm`). Both
    return a WGS84 `~pyramids.feature.collection.FeatureCollection`; an empty
    layer yields a schema-only collection.

    Args:
        path: Local path to the `.osm.pbf` extract.
        pyrosm_method: The `pyrosm.OSM` reader method for the layer
            (`get_buildings`, `get_network`, `get_pois`, `get_landuse`,
            `get_natural`, `get_boundaries`).
        network_type: The `get_network(network_type=...)` argument (e.g.
            `"driving"`); used only when `pyrosm_method` is `get_network`.
        bbox: Optional `(west, south, east, north)` clip. `pyrosm` clips at read
            time; the `pyosmium` path clips the built geometry.
        engine: `"pyrosm"` (in-memory, default) or `"pyosmium"` (streaming).

    Returns:
        FeatureCollection: The layer's features, CRS `EPSG:4326`.

    Raises:
        ImportError: If the selected engine's SDK is not installed
            (`earthlens[osm-pbf]`).
        ValueError: If `engine="pyrosm"` is used on a file larger than
            `MAX_PYROSM_BYTES`, or `engine` is not a known value.
    """
    path = Path(path)
    if engine == "pyrosm":
        return _read_pyrosm(path, pyrosm_method, network_type, bbox)
    if engine == "pyosmium":
        return _read_pyosmium(path, pyrosm_method, bbox)
    raise ValueError(
        f"engine must be 'pyrosm' or 'pyosmium', got {engine!r}."
    )


def _read_pyrosm(
    path: Path,
    pyrosm_method: str,
    network_type: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> FeatureCollection:
    """Read a layer with the in-memory `pyrosm` engine.

    Args:
        path: Local `.osm.pbf` path.
        pyrosm_method: The `pyrosm.OSM` reader method name.
        network_type: The `get_network` road subset, if applicable.
        bbox: Optional `(west, south, east, north)` read-time clip.

    Returns:
        FeatureCollection: The layer's features (empty schema-only when the
            method returns no rows).

    Raises:
        ImportError: If `pyrosm` is not installed.
        ValueError: If the extract exceeds `MAX_PYROSM_BYTES`.
    """
    size = path.stat().st_size
    if size > MAX_PYROSM_BYTES:
        raise ValueError(
            f"{path.name} is {size / 1024**3:.1f} GB, too large for the in-memory "
            f"'pyrosm' engine (cap {MAX_PYROSM_BYTES / 1024**3:.0f} GB). Read it "
            "with engine='pyosmium' (streaming) instead."
        )
    try:
        from pyrosm import OSM as PyrosmOSM
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "The OSM pbf protocol requires the `pyrosm` SDK. Install it with "
            "`pip install earthlens[osm-pbf]`."
        ) from exc

    bounding_box = None
    if bbox is not None:
        from shapely.geometry import box

        west, south, east, north = bbox
        bounding_box = box(west, south, east, north)
    reader = PyrosmOSM(str(path), bounding_box=bounding_box)
    method = getattr(reader, pyrosm_method)
    gdf = method(network_type=network_type) if pyrosm_method == "get_network" else method()
    if gdf is None or len(gdf) == 0:
        return empty_fc()
    return to_fc(gdf)


def _read_pyosmium(
    path: Path,
    pyrosm_method: str,
    bbox: tuple[float, float, float, float] | None,
) -> FeatureCollection:
    """Read a layer with the streaming `pyosmium` engine (bounded memory).

    Streams the extract once, keeping the layer's primary geometry kind under a
    single representative tag key (see `_PYOSMIUM_LAYERS`): tagged nodes →
    `Point`, tagged ways → `LineString`, tagged areas → polygon. A `bbox` clips
    the built geometry (post-hoc shapely intersection). This is the fallback for
    files too large for `pyrosm`; it is coarser than the `pyrosm` path by
    design (one geometry kind, one tag key per layer).

    Args:
        path: Local `.osm.pbf` path.
        pyrosm_method: The layer's `pyrosm` method name (mapped to a tag key +
            geometry strategy via `_PYOSMIUM_LAYERS`).
        bbox: Optional `(west, south, east, north)` clip.

    Returns:
        FeatureCollection: The layer's features (empty schema-only when nothing
            matched).

    Raises:
        ImportError: If `osmium` (`pyosmium`) is not installed.
        ValueError: If `pyrosm_method` has no `pyosmium` read plan.
    """
    try:
        import osmium
        import shapely.wkb
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "The OSM pbf protocol's pyosmium engine requires the `osmium` "
            "(pyosmium) SDK. Install it with `pip install earthlens[osm-pbf]`."
        ) from exc

    plan = _PYOSMIUM_LAYERS.get(pyrosm_method)
    if plan is None:
        raise ValueError(
            f"no pyosmium read plan for {pyrosm_method!r}; known layers: "
            f"{sorted(_PYOSMIUM_LAYERS)}."
        )
    key, strategy = plan
    clip = None
    if bbox is not None:
        from shapely.geometry import box

        clip = box(*bbox)

    factory = osmium.geom.WKBFactory()
    rows: list[dict] = []
    for osm_id, osm_type, geometry in _stream_geometries(
        osmium, shapely.wkb, factory, str(path), key, strategy
    ):
        if clip is not None and not geometry.intersects(clip):
            continue
        rows.append({"osm_id": osm_id, "osm_type": osm_type, "geometry": geometry})
    if not rows:
        return empty_fc()

    import geopandas as gpd

    return to_fc(gpd.GeoDataFrame(rows, geometry="geometry", crs=OSM_CRS))


def _stream_geometries(osmium, shapely_wkb, factory, path, key, strategy):
    """Yield `(osm_id, osm_type, shapely_geometry)` for one layer via streaming.

    Selects the `osmium.FileProcessor` configuration for the strategy — node
    locations for lines, area assembly for areas — filtered to `key`, and
    converts each matched object to a shapely geometry through the WKB factory.

    Args:
        osmium: The imported `osmium` module.
        shapely_wkb: The imported `shapely.wkb` module.
        factory: An `osmium.geom.WKBFactory`.
        path: Local `.osm.pbf` path (string).
        key: The OSM tag key to filter on (e.g. `"building"`).
        strategy: `"point"`, `"line"`, or `"area"`.

    Yields:
        tuple[int, str, shapely.geometry.base.BaseGeometry]: One record per
            matched object.
    """
    key_filter = osmium.filter.KeyFilter(key)
    if strategy == "point":
        from shapely.geometry import Point

        processor = osmium.FileProcessor(path).with_filter(key_filter)
        for obj in processor:
            if obj.is_node() and obj.location.valid():
                yield obj.id, "node", Point(obj.location.lon, obj.location.lat)
    elif strategy == "line":
        processor = osmium.FileProcessor(path).with_locations().with_filter(key_filter)
        for obj in processor:
            if obj.is_way():
                geom = _wkb_geometry(shapely_wkb, factory.create_linestring, obj)
                if geom is not None:
                    yield obj.id, "way", geom
    else:  # area
        processor = osmium.FileProcessor(path).with_areas().with_filter(key_filter)
        for obj in processor:
            if isinstance(obj, osmium.osm.Area):
                geom = _wkb_geometry(shapely_wkb, factory.create_multipolygon, obj)
                if geom is not None:
                    osm_type = "way" if obj.from_way() else "relation"
                    yield obj.orig_id(), osm_type, geom


def _wkb_geometry(shapely_wkb, create, obj):
    """Build a shapely geometry from an osmium object, or `None` on failure.

    A degenerate object (an unclosed way passed to the multipolygon factory, a
    way with too few located nodes) raises inside the WKB factory; that object
    is skipped rather than failing the whole read.

    Args:
        shapely_wkb: The imported `shapely.wkb` module.
        create: The `WKBFactory` method to call (`create_linestring` /
            `create_multipolygon`).
        obj: The osmium object.

    Returns:
        shapely.geometry.base.BaseGeometry | None: The geometry, or `None` when
            it could not be built.
    """
    try:
        return shapely_wkb.loads(create(obj), hex=True)
    except Exception:  # noqa: BLE001 - a single degenerate object must not fail the read
        return None
