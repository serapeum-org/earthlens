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
from datetime import datetime

import requests

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
    """Return the body of a GET as text (jeodpp autoindex / coastal CSV)."""
    response = requests.get(url, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return response.text


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
    """Compact `YYYYMMDDTHH` id from a `.../YYYY/MM/DD/HH/` cycle URL."""
    parts = [p for p in url.strip("/").split("/") if p.isdigit()]
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


def _descend_newest(
    url: str, level: int, endfls_marker: str, http_text
) -> tuple[str, str] | None:
    """Depth-first descend `level` numeric dirs, returning the newest complete cycle."""
    for name in _numeric_dirs(list_directory(url, http_text=http_text)):
        child = f"{url}{name}/"
        if level == 1:
            if endfls_marker in list_directory(child, http_text=http_text):
                return child, _cycle_id(child)
        else:
            found = _descend_newest(child, level - 1, endfls_marker, http_text)
            if found is not None:
                return found
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
        found = _descend_newest(f"{root}/", 4, endfls_marker, http_text=http_text)
        if found is None:
            raise ValueError(
                f"no complete cycle (with {endfls_marker!r}) found under {root}."
            )
        return found
    dt = _parse_reference_time(reference_time)
    cycle_url = f"{root}/{dt.strftime(cycle_path_template)}/"
    if endfls_marker not in list_directory(cycle_url, http_text=http_text):
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
        if not name.endswith("/") and fnmatch.fnmatch(name, glob):
            return name
    raise ValueError(f"no file matching {glob!r} in {cycle_url}.")


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
    """Shift a geotransform's origin to a pixel window's top-left corner."""
    x0, dx, _, y0, _, dy = geo
    return (x0 + col_off * dx, dx, 0.0, y0 + row_off * dy, 0.0, dy)
