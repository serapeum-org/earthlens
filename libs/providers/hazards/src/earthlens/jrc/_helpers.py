"""URL + cycle-resolution helpers for the JRC backend (EFHM + sea-level).

The EFHM half serves one whole-Europe GeoTIFF per return period at a
deterministic `{BASE_URL}/Europe_RP{rp}_filled_depth.tif`. The sea-level half
walks the jeodpp autoindex (`YYYY/MM/DD/HH` cycle tree) to resolve a forecast
cycle — gated on the 0-byte `endFls` sentinel — and reconstructs the global
0.25 deg north-up affine the gridded NetCDF variables need (they arrive
index-space over `/vsicurl`; this is the interim until pyramids#1071 builds the
affine from the CF `latitude` / `longitude` coordinates). All network access
goes through the injectable `http_text` seam so tests can fake the autoindex.
"""

from __future__ import annotations

import fnmatch
import math
import re
from datetime import datetime, timedelta
from functools import lru_cache

import numpy as np
import requests
from loguru import logger

#: Root of the JRC CEMS-EFAS flood-hazard directory (anonymous HTTPS, no auth).
BASE_URL: str = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard"
)

#: `strftime`-free file-name template; `{rp}` is the integer return period.
FILENAME_TEMPLATE: str = "Europe_RP{rp}_filled_depth.tif"

#: Seconds to wait on a jeodpp autoindex / coastal-CSV request.
_HTTP_TIMEOUT: float = 60.0

#: Captures the `href` targets in the jeodpp Apache autoindex.
_HREF = re.compile(r'href="([^"]+)"')


@lru_cache(maxsize=1)
def _client():
    """Return the process-wide `HttpClient` used for every jeodpp request.

    Cached so one `latest` resolve reuses a single session (and its connection
    pool) across the whole directory walk instead of building one per request.

    Returns:
        earthlens.base.http.HttpClient: The shared client.
    """
    from earthlens.base.http import HttpClient

    return HttpClient(timeout=_HTTP_TIMEOUT)


def efhm_url(
    rp: int, *, base_url: str = BASE_URL, template: str = FILENAME_TEMPLATE
) -> str:
    """Build the EFHM GeoTIFF URL for one return period.

    Args:
        rp: The integer return period in years (e.g. `100`).
        base_url: The directory root; defaults to `BASE_URL`.
        template: The file-name template; defaults to `FILENAME_TEMPLATE`.

    Returns:
        str: The fully-qualified `.tif` URL.

    Examples:
        - The verified RP100 URL:
            ```python
            >>> from earthlens.jrc._helpers import efhm_url
            >>> efhm_url(100)
            'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard/Europe_RP100_filled_depth.tif'

            ```
    """
    return f"{base_url}/{template.format(rp=rp)}"


def _http_text(url: str) -> str:
    """Return the body of a GET as text (jeodpp autoindex / coastal CSV).

    Reads through the shared `HttpClient` so the crawl inherits the repo's
    session reuse, user agent and `Retry-After`-aware retry/back-off.

    Args:
        url: The directory or file URL to fetch.

    Returns:
        str: The response body as text.

    Raises:
        requests.HTTPError: If the server returns a non-2xx status.
    """
    return str(_client().get(url).text)


def http_bytes(url: str) -> bytes:
    """Return the raw body of a GET, undecoded.

    The coastal CSV is served as `text/csv` with **no charset**, so `requests`
    falls back to ISO-8859-1 and silently mangles the UTF-8 country names
    (`Côte d'Ivoire`, `São Tomé and Príncipe`, `Åland`). Handing the bytes to the
    parser lets it decode UTF-8 properly.

    Args:
        url: The file URL to fetch.

    Returns:
        bytes: The undecoded response body.

    Raises:
        requests.HTTPError: If the server returns a non-2xx status.
    """
    return bytes(_client().get(url).content)


def list_directory(url: str, *, http_text=_http_text) -> list[str]:
    """List the entries of a jeodpp autoindex directory.

    Args:
        url: The directory URL (a trailing slash is added when missing).
        http_text: Injectable text fetcher (tests pass a fake).

    Returns:
        list[str]: Entry names — subdirectories keep their trailing `/`; the
            parent link and the column-sort query links are dropped.
    """
    if not url.endswith("/"):
        url += "/"
    names: list[str] = []
    for href in _HREF.findall(http_text(url)):
        href = href.strip()
        if not href or href.startswith(("?", "/", "..")):
            continue
        names.append(href)
    return names


def _numeric_dirs(names: list[str]) -> list[str]:
    """Return the numeric subdirectory names, newest (largest) first."""
    dirs = [n[:-1] for n in names if n.endswith("/") and n[:-1].isdigit()]
    return sorted(dirs, key=int, reverse=True)


def _cycle_id(url: str) -> str:
    """Compact `YYYYMMDDTHH` id from a `.../YYYY/MM/DD/HH/` cycle URL.

    Args:
        url: The resolved cycle directory URL.

    Returns:
        str: The `YYYYMMDDTHH` identifier used in output filenames.

    Raises:
        ValueError: If the URL does not carry the four numeric path segments.
    """
    parts = [p for p in url.strip("/").split("/") if p.isdigit()]
    if len(parts) < 4:
        raise ValueError(
            f"cannot derive a cycle id from {url!r}: expected a "
            ".../YYYY/MM/DD/HH/ path."
        )
    year, month, day, hour = parts[-4:]
    return f"{year}{month}{day}T{hour}"


def _is_latest(value) -> bool:
    """Whether a `reference_time` means 'the newest complete cycle'."""
    return value is None or (
        isinstance(value, str) and value.strip().lower() in ("", "latest")
    )


def _parse_reference_time(value) -> datetime:
    """Parse an explicit `reference_time` to a `datetime`.

    Args:
        value: A `datetime`, or a string such as `"2026-08-26T12"`,
            `"2026-08-26 12"`, `"2026-08-26"`, or `"20260826T12"`.

    Returns:
        datetime: The parsed cycle timestamp.

    Raises:
        ValueError: If the value cannot be parsed.
    """
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y%m%dT%H", "%Y%m%d%H"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"could not parse reference_time {value!r} (expected e.g. '2026-08-26T12')."
    )


#: Cap on the directory listings issued while resolving `"latest"`. Without it a
#: renamed sentinel or a publishing pause turns the walk into a crawl of the whole
#: multi-year archive. This bounds EVERY listing (year/month/day levels included),
#: not just the leaf probes — budgeting leaves alone still allows thousands of
#: requests across the intermediate levels.
MAX_CYCLE_PROBES: int = 60


def _probe_leaf(
    url: str, endfls_marker: str, http_text, budget: list[int]
) -> tuple[str, str] | None:
    """Spend one budget unit checking whether a cycle folder is complete.

    Args:
        url: The candidate cycle directory (trailing `/`).
        endfls_marker: The cycle-complete sentinel to look for.
        http_text: Injectable text fetcher.
        budget: The shared remaining-listing allowance, decremented in place.

    Returns:
        tuple[str, str] | None: `(cycle_url, cycle_id)` when the cycle carries the
            sentinel, otherwise `None`.
    """
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    if endfls_marker in list_directory(url, http_text=http_text):
        return url, _cycle_id(url)
    return None


def _descend_newest(
    url: str, level: int, endfls_marker: str, http_text, budget: list[int]
) -> tuple[str, str] | None:
    """Depth-first descend `level` numeric dirs, returning the newest complete cycle.

    Args:
        url: The directory to descend from (trailing `/`).
        level: How many numeric levels remain below `url`.
        endfls_marker: The cycle-complete sentinel to look for at the leaf.
        http_text: Injectable text fetcher.
        budget: One-element list holding the remaining leaf probes; decremented in
            place so the whole recursion shares one allowance.

    Returns:
        tuple[str, str] | None: `(cycle_url, cycle_id)` for the newest complete
            cycle, or `None` when none was found within the budget.
    """
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    for name in _numeric_dirs(list_directory(url, http_text=http_text)):
        child = f"{url}{name}/"
        found = (
            _probe_leaf(child, endfls_marker, http_text, budget)
            if level == 1
            else _descend_newest(child, level - 1, endfls_marker, http_text, budget)
        )
        if found is not None:
            return found
        if budget[0] <= 0:
            return None
    return None


def resolve_cycle(
    base_url: str,
    product: str,
    cycle_path_template: str,
    reference_time,
    endfls_marker: str,
    *,
    http_text=_http_text,
) -> tuple[str, str]:
    """Resolve a forecast cycle to its directory URL, gated on `endFls`.

    Args:
        base_url: The sea-level `probabilistic_data_driven` root.
        product: The product subdir (`"medium_term_forecasts"` /
            `"subseasonal_forecasts"`).
        cycle_path_template: `strftime` layout of the cycle folders under
            `product` (`"%Y/%m/%d/%H"`).
        reference_time: `"latest"` (default) or an explicit cycle.
        endfls_marker: The 0-byte cycle-complete sentinel name (`"endFls"`).
        http_text: Injectable text fetcher.

    Returns:
        tuple[str, str]: `(cycle_url, cycle_id)` — the directory URL (trailing
            `/`) and a compact `YYYYMMDDTHH` id.

    Raises:
        ValueError: If no complete cycle is found, or a requested one is
            missing / not yet complete.
    """
    root = f"{base_url.rstrip('/')}/{product}"
    if _is_latest(reference_time):
        budget = [MAX_CYCLE_PROBES]
        found = _descend_newest(f"{root}/", 4, endfls_marker, http_text, budget)
        if found is None:
            raise ValueError(
                f"no complete cycle (with {endfls_marker!r}) found under {root} "
                f"within the newest {MAX_CYCLE_PROBES} cycle folders; the archive "
                "may be mid-publish or the sentinel may have been renamed."
            )
        return found
    dt = _parse_reference_time(reference_time)
    cycle_url = f"{root}/{dt.strftime(cycle_path_template)}/"
    try:
        entries = list_directory(cycle_url, http_text=http_text)
    except requests.HTTPError as exc:
        # Only a 404 means "no such cycle" (the archive keeps a rolling window, so
        # an aged-out or mistyped cycle is the common case). A 403/429/5xx is a
        # live server problem and must not be reported as a missing cycle.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and status != 404:
            raise
        raise ValueError(
            f"cycle {dt.strftime(cycle_path_template)} is not published at "
            f"{cycle_url} — it may have aged out of the archive's retention "
            "window, or the cycle hour may be wrong."
        ) from exc
    if endfls_marker not in entries:
        raise ValueError(
            f"cycle {dt.strftime(cycle_path_template)} is not complete "
            f"(no {endfls_marker!r} at {cycle_url})."
        )
    return cycle_url, dt.strftime("%Y%m%dT%H")


def find_cycle_file(cycle_url: str, glob: str, *, http_text=_http_text) -> str:
    """Return the name of the file in `cycle_url` matching `glob`.

    Args:
        cycle_url: The resolved cycle directory URL.
        glob: A `fnmatch` pattern (`"*TWLforecastGridded_*.nc"`).
        http_text: Injectable text fetcher.

    Returns:
        str: The matching file name.

    Raises:
        ValueError: If no file matches (the filename embeds a start-end range,
            so it is read from the listing rather than reconstructed).
    """
    for name in list_directory(cycle_url, http_text=http_text):
        if not name.endswith("/") and fnmatch.fnmatchcase(name, glob):
            return name
    raise ValueError(f"no file matching {glob!r} in {cycle_url}.")


#: CF epoch of the cubes' `time` coordinate (`days since 1950-01-01`).
_TIME_EPOCH = datetime(1950, 1, 1)


def band_valid_times(url: str, steps: int) -> list[str]:
    """Name each output band by the forecast valid time it holds.

    The cubes carry a CF `time` coordinate (`days since 1950-01-01`) that becomes
    the written GeoTIFF's band axis; without these names a band is unidentifiable
    without going back to the source. `time` is a *coordinate*, so it is reachable
    through GDAL's multidimensional API rather than the container's data variables.

    Args:
        url: The `/vsicurl/`-prefixed cube URL to read the coordinate from.
        steps: How many bands the written raster has.

    Returns:
        list[str]: One `YYYY-MM-DDTHH:MM` label per band, or a positional
            `step_<n>` fallback when the coordinate cannot be read.
    """
    try:
        gdal = gdal_module()

        dataset = gdal.OpenEx(url, gdal.OF_MULTIDIM_RASTER)
        try:
            axis = np.asarray(
                dataset.GetRootGroup().OpenMDArray("time").ReadAsArray()
            ).ravel()
        finally:
            # Release the second remote handle this opens; leaving it to the GC
            # holds a /vsicurl connection open per fetch.
            dataset = None
        # Compare the FULL axis, unsliced: a 2-D aggregate field (e.g. a 15-day
        # exceedance probability) has one band while the cube's time axis has
        # many, and slicing first would confidently mislabel it with step 0's
        # timestamp. Only a field whose bands ARE the time axis gets valid times.
        if axis.size == steps:
            return [
                (_TIME_EPOCH + timedelta(days=float(v))).strftime("%Y-%m-%dT%H:%M")
                for v in axis
            ]
    except Exception:  # noqa: BLE001 - naming is best-effort; never fail the fetch
        logger.warning(
            "JRC: could not read the cube's time axis; bands fall back to "
            "positional step_N names."
        )
    return [f"step_{index + 1}" for index in range(steps)]


def gdal_module():
    """Return the vendored `osgeo.gdal` module (an injectable seam for tests)."""
    from osgeo import gdal

    return gdal


def _read_grid_coordinates(url: str):
    """Return the cube's `(longitude, latitude)` arrays, or `None` if unreadable."""
    try:
        gdal = gdal_module()

        dataset = gdal.OpenEx(url, gdal.OF_MULTIDIM_RASTER)
        try:
            root = dataset.GetRootGroup()
            return (
                np.asarray(root.OpenMDArray("longitude").ReadAsArray()).ravel(),
                np.asarray(root.OpenMDArray("latitude").ReadAsArray()).ravel(),
            )
        finally:
            dataset = None
    except Exception:  # noqa: BLE001 - a missing coordinate is not a crop error
        return None


def verify_grid_against_coordinates(
    url: str,
    geo: tuple[float, float, float, float, float, float],
    cols: int,
    rows: int,
    *,
    tolerance: float = 0.02,
) -> None:
    """Check a reconstructed affine against the cube's own CF coordinates.

    `grid_geotransform` derives the origin, cell size and orientation from the
    grid *shape* alone. This confirms the file really is the global grid that
    assumes: the coordinate arrays must span the same extent, and the array must
    be north-up once read (the source stores `latitude` ascending, so this
    depends on GDAL's CF flip staying in place).

    Args:
        url: The `/vsicurl/`-prefixed cube URL.
        geo: The reconstructed geotransform.
        cols: The variable's column count.
        rows: The variable's row count.
        tolerance: Allowed degrees of slack on each edge (half a cell by default).

    Raises:
        ValueError: If the file's coordinates contradict the reconstructed grid.
    """
    coordinates = _read_grid_coordinates(url)
    if coordinates is None:
        logger.debug("JRC: could not read the cube's coordinates to verify the grid")
        return
    lon, lat = coordinates

    if lon.size != cols or lat.size != rows:
        raise ValueError(
            f"the cube's coordinates ({lon.size}x{lat.size}) do not match the "
            f"variable's grid ({cols}x{rows}); the reconstructed affine cannot be "
            "trusted."
        )
    x0, dx, _, y0, _, _ = geo
    half = abs(dx) / 2.0
    west, east = float(lon.min()) - half, float(lon.max()) + half
    south, north = float(lat.min()) - half, float(lat.max()) + half
    if abs(west - x0) > tolerance or abs(north - y0) > tolerance:
        raise ValueError(
            f"the cube spans lon {west:.3f}..{east:.3f} / lat {south:.3f}.."
            f"{north:.3f}, which contradicts the assumed global grid starting at "
            f"({x0}, {y0}). The affine reconstruction needs updating."
        )


def grid_geotransform(
    cols: int, rows: int
) -> tuple[float, float, float, float, float, float]:
    """Build the global north-up affine from a gridded variable's shape.

    Interim reconstruction until pyramids#1071 builds the affine from the CF
    `latitude` / `longitude` coordinates: the sea-level cubes are global
    (lon -180..180, lat 90..-90), so the geotransform follows from the shape.

    Args:
        cols: Grid column count (1440 at 0.25 deg).
        rows: Grid row count (720 at 0.25 deg).

    Returns:
        tuple: The 6-element north-up geotransform
            `(-180, 360/cols, 0, 90, 0, -180/rows)`.
    """
    return (-180.0, 360.0 / cols, 0.0, 90.0, 0.0, -180.0 / rows)


def pixel_window(
    geo: tuple[float, float, float, float, float, float],
    bbox,
    cols: int,
    rows: int,
) -> tuple[int, int, int, int] | None:
    """Map an AOI bbox to a clamped pixel window on a north-up grid.

    Args:
        geo: The source geotransform (`grid_geotransform`).
        bbox: `(west, south, east, north)` in degrees.
        cols: Grid column count.
        rows: Grid row count.

    Returns:
        tuple[int, int, int, int] | None: `(col_off, row_off, width, height)`,
            or `None` when the bbox does not overlap the grid.
    """
    x0, dx, _, y0, _, dy = geo
    west, south, east, north = bbox
    pixel_h = -dy
    col_off = max(0, math.floor((west - x0) / dx))
    col_end = min(cols, math.ceil((east - x0) / dx))
    row_off = max(0, math.floor((y0 - north) / pixel_h))
    row_end = min(rows, math.ceil((y0 - south) / pixel_h))
    if col_end <= col_off or row_end <= row_off:
        return None
    return col_off, row_off, col_end - col_off, row_end - row_off


def window_origin(
    geo: tuple[float, float, float, float, float, float], col_off: int, row_off: int
) -> tuple[float, float, float, float, float, float]:
    """Shift a geotransform's origin to a pixel window's top-left corner.

    Args:
        geo: The source grid's 6-element north-up geotransform.
        col_off: The window's first column, in source pixels.
        row_off: The window's first row, in source pixels.

    Returns:
        tuple: The same transform with its origin moved to the window corner, so
            the cropped array carries real-world coordinates.

    Examples:
        - The North Sea window of the global 0.25 deg grid starts at 3E / 53N:
            ```python
            >>> from earthlens.jrc._helpers import grid_geotransform, window_origin
            >>> window_origin(grid_geotransform(1440, 720), 732, 148)
            (3.0, 0.25, 0.0, 53.0, 0.0, -0.25)

            ```
    """
    x0, dx, _, y0, _, dy = geo
    return (x0 + col_off * dx, dx, 0.0, y0 + row_off * dy, 0.0, dy)
