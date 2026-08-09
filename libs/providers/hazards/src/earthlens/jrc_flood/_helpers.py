"""URL builder + pixel-window math for the JRC European flood-hazard backend.

The JRC serves the European Flood Hazard Map (EFHM) over a deterministic,
anonymous HTTPS directory (verified live 2026-08-09): one whole-Europe GeoTIFF
per return period at `{BASE_URL}/Europe_RP{rp}_filled_depth.tif`. Each file is a
single-band EPSG:4326 Float32 grid at ~0.000833° (~90 m; documented 100 m),
covering Europe and the Mediterranean Basin — 110162×51992 px (~23 GB
uncompressed), so it is **never** read whole. Instead the backend opens it
lazily over GDAL's `/vsicurl` (HTTP range requests) and reads only the AOI's
pixel window; `pixel_window` turns a geographic bbox into that window.
"""

from __future__ import annotations

import math

#: Root of the JRC CEMS-EFAS flood-hazard directory (anonymous HTTPS, no auth).
BASE_URL: str = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard"
)

#: `strftime`-free file-name template; `{rp}` is the integer return period.
FILENAME_TEMPLATE: str = "Europe_RP{rp}_filled_depth.tif"


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
            >>> from earthlens.jrc_flood._helpers import efhm_url
            >>> efhm_url(100)
            'https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/flood_hazard/Europe_RP100_filled_depth.tif'

            ```
    """
    return f"{base_url}/{template.format(rp=rp)}"


def pixel_window(
    geotransform: tuple[float, float, float, float, float, float],
    bbox: tuple[float, float, float, float],
    columns: int,
    rows: int,
) -> tuple[int, int, int, int] | None:
    """Convert a geographic bbox to a clamped `(col_off, row_off, cols, rows)` window.

    Uses the affine `geotransform` (`ox, px, 0, oy, 0, py`; `py` is negative for
    a north-up raster) to map the bbox corners to pixel indices, then clamps the
    window to the raster's `columns` × `rows` extent. Returns `None` when the
    bbox does not overlap the raster (an AOI outside the EFHM's Europe /
    Mediterranean coverage).

    Args:
        geotransform: The GDAL affine transform `(ox, px, 0, oy, 0, py)`.
        bbox: `(west, south, east, north)` in the raster's CRS (EPSG:4326).
        columns: Raster width in pixels.
        rows: Raster height in pixels.

    Returns:
        tuple[int, int, int, int] | None: The `(col_off, row_off, cols, rows)`
            window, or `None` when the bbox is entirely outside the raster.

    Examples:
        - A small AOI maps to a small window:
            ```python
            >>> from earthlens.jrc_flood._helpers import pixel_window
            >>> gt = (-24.54208333, 0.0008333333333333334, 0.0,
            ...       71.13375, 0.0, -0.0008333333333333334)
            >>> pixel_window(gt, (4.8, 51.8, 5.0, 52.0), 110162, 51992)
            (35210, 22960, 241, 241)

            ```
        - An AOI south of the coverage returns `None`:
            ```python
            >>> from earthlens.jrc_flood._helpers import pixel_window
            >>> gt = (-24.54208333, 0.0008333333333333334, 0.0,
            ...       71.13375, 0.0, -0.0008333333333333334)
            >>> pixel_window(gt, (4.8, -5.0, 5.0, -4.8), 110162, 51992) is None
            True

            ```
    """
    west, south, east, north = bbox
    ox, px, _, oy, _, py = geotransform
    col_start = math.floor((west - ox) / px)
    col_stop = math.ceil((east - ox) / px)
    # `py` is negative, so the northern edge maps to the smaller row index.
    row_start = math.floor((north - oy) / py)
    row_stop = math.ceil((south - oy) / py)
    col_off = max(0, col_start)
    row_off = max(0, row_start)
    col_end = min(columns, col_stop)
    row_end = min(rows, row_stop)
    cols = col_end - col_off
    rows_out = row_end - row_off
    if cols <= 0 or rows_out <= 0:
        return None
    return (col_off, row_off, cols, rows_out)


def window_origin(
    geotransform: tuple[float, float, float, float, float, float],
    col_off: int,
    row_off: int,
) -> tuple[float, float, float, float, float, float]:
    """Return the geotransform of a window, shifted to its top-left pixel.

    Args:
        geotransform: The source affine transform `(ox, px, 0, oy, 0, py)`.
        col_off: The window's left pixel column.
        row_off: The window's top pixel row.

    Returns:
        tuple: The window's affine transform (same pixel sizes, shifted origin).
    """
    ox, px, rot1, oy, rot2, py = geotransform
    return (ox + col_off * px, px, rot1, oy + row_off * py, rot2, py)
