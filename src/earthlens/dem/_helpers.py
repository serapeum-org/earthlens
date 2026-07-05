"""Pure tile-key arithmetic for the Copernicus DEM buckets.

The Copernicus DEM COGs live one per 1° x 1° tile on the anonymous AWS
buckets. Every tile is addressed by its SW-corner integer degrees on a
fixed grid, encoded in the object name as `N##`/`S##` for latitude
(2-digit) and `E###`/`W###` for longitude (3-digit). This module is the
one place that arithmetic lives, so the backend, the tests, and the
gated e2e all resolve a bbox to the same list of tile keys.

Nothing here touches the network — the enumeration is deterministic
integer arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["Tile", "bbox_to_tiles", "tile_key", "tile_name"]


@dataclass(frozen=True)
class Tile:
    """One Copernicus DEM 1° x 1° tile addressed by its SW corner.

    Attributes:
        lat: SW-corner latitude in integer degrees, in `[-90, 89]`.
        lon: SW-corner longitude in integer degrees, in `[-180, 179]`.

    Examples:
        - Nile Delta cell (30 N, 31 E):
            ```python
            >>> from earthlens.dem._helpers import Tile
            >>> t = Tile(lat=30, lon=31)
            >>> t.lat, t.lon
            (30, 31)

            ```
    """

    lat: int
    lon: int


def _fmt_lat(lat: int) -> str:
    """Format an integer latitude as `N##` / `S##` (2-digit)."""
    hemi = "N" if lat >= 0 else "S"
    return f"{hemi}{abs(lat):02d}"


def _fmt_lon(lon: int) -> str:
    """Format an integer longitude as `E###` / `W###` (3-digit)."""
    hemi = "E" if lon >= 0 else "W"
    return f"{hemi}{abs(lon):03d}"


def tile_name(tile: Tile, resolution_token: str) -> str:
    """Return the tile identifier (also the top-level bucket "folder").

    The identifier follows the Copernicus naming convention verified
    against the live buckets:
    `Copernicus_DSM_COG_{token}_{LAT}_00_{LON}_00_DEM`, where the
    `_00_` markers are the arc-minute portion of the corner (always
    `00` on the 1° grid).

    Args:
        tile: The tile addressed by its SW-corner integer degrees.
        resolution_token: `"10"` for GLO-30 or `"30"` for GLO-90 —
            the token embedded in the tile name, taken from the
            catalog row.

    Returns:
        str: The tile identifier without a trailing suffix.

    Examples:
        - GLO-30 Nile Delta (30 N, 31 E):
            ```python
            >>> from earthlens.dem._helpers import Tile, tile_name
            >>> tile_name(Tile(lat=30, lon=31), "10")
            'Copernicus_DSM_COG_10_N30_00_E031_00_DEM'

            ```
    """
    return (
        f"Copernicus_DSM_COG_{resolution_token}_"
        f"{_fmt_lat(tile.lat)}_00_{_fmt_lon(tile.lon)}_00_DEM"
    )


def tile_key(tile: Tile, resolution_token: str) -> str:
    """Return the bucket-relative object key for the tile's DEM COG.

    A tile directory in the bucket carries the DEM `.tif` at
    `<name>/<name>.tif` plus a set of sidecars (`AUXFILES/`,
    `PREVIEW/`, `INFO/`) that the DEM backend deliberately ignores.

    Args:
        tile: The tile addressed by its SW-corner integer degrees.
        resolution_token: `"10"` for GLO-30, `"30"` for GLO-90.

    Returns:
        str: The bucket-relative key to the DEM COG.

    Examples:
        - GLO-30 Nile Delta:
            ```python
            >>> from earthlens.dem._helpers import Tile, tile_key
            >>> tile_key(Tile(lat=30, lon=31), "10")
            'Copernicus_DSM_COG_10_N30_00_E031_00_DEM/Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif'

            ```
    """
    name = tile_name(tile, resolution_token)
    return f"{name}/{name}.tif"


def bbox_to_tiles(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
) -> list[Tile]:
    """Enumerate every 1° tile whose SW corner lies inside the bbox.

    Snaps the bbox to the integer-degree tile grid: for each axis the
    minimum corner is floored and the maximum corner is included up to
    (and excluding) the ceiling. A bbox that lies entirely inside a
    single tile still returns that tile. Latitude and longitude values
    are clamped to the WGS84 grid range.

    Antimeridian-straddling bboxes (`lon_min > lon_max`) are not
    supported in this first cut — pass the two halves in separate calls.

    Args:
        lat_min: Southern edge of the bbox in degrees.
        lat_max: Northern edge of the bbox in degrees.
        lon_min: Western edge of the bbox in degrees.
        lon_max: Eastern edge of the bbox in degrees.

    Returns:
        list[Tile]: Every 1° tile whose SW corner lies within the
            bbox, in row-major order (south -> north, then west ->
            east).

    Raises:
        ValueError: If `lat_min > lat_max`, `lon_min > lon_max`, or an
            axis value is outside the WGS84 range.

    Examples:
        - A single tile bbox:
            ```python
            >>> from earthlens.dem._helpers import bbox_to_tiles
            >>> [(t.lat, t.lon) for t in bbox_to_tiles(30.2, 30.8, 31.2, 31.8)]
            [(30, 31)]

            ```
    """
    if lat_min > lat_max:
        raise ValueError(f"lat_min {lat_min} > lat_max {lat_max}")
    if lon_min > lon_max:
        raise ValueError(
            f"lon_min {lon_min} > lon_max {lon_max} — antimeridian-straddling "
            "bboxes are not supported; pass the two halves in separate calls."
        )
    if not (-90.0 <= lat_min <= 90.0 and -90.0 <= lat_max <= 90.0):
        raise ValueError(
            f"latitude out of [-90, 90]: lat_min={lat_min}, lat_max={lat_max}"
        )
    if not (-180.0 <= lon_min <= 180.0 and -180.0 <= lon_max <= 180.0):
        raise ValueError(
            f"longitude out of [-180, 180]: lon_min={lon_min}, lon_max={lon_max}"
        )

    lat_origins = _axis_origins(lat_min, lat_max, min_index=-90, max_index=89)
    lon_origins = _axis_origins(lon_min, lon_max, min_index=-180, max_index=179)
    return [Tile(lat=lat, lon=lon) for lat in lat_origins for lon in lon_origins]


def _axis_origins(
    low: float, high: float, *, min_index: int, max_index: int
) -> list[int]:
    """Integer tile-origin coordinates covering `[low, high]` on one axis.

    A tile at index `N` covers the half-open interval `[N, N+1)`, so a
    bbox whose upper edge sits exactly on an integer degree does NOT
    require the tile starting at that degree (nothing above the edge
    lies inside the bbox). The `stop = math.ceil(high) - 1` computation
    below excludes that boundary tile.

    Args:
        low: Lower edge in degrees.
        high: Upper edge in degrees.
        min_index: Inclusive lower bound on the returned indices.
        max_index: Inclusive upper bound on the returned indices.

    Returns:
        list[int]: Integer origins spanning the axis, clamped to the
            grid range.
    """
    start = max(math.floor(low), min_index)
    stop = min(math.ceil(high) - 1, max_index)
    if stop < start:
        # A zero-width bbox on an exact degree boundary (e.g. `[3.0, 3.0]`)
        # still needs the tile that boundary lies inside.
        stop = start
    return list(range(start, stop + 1))
