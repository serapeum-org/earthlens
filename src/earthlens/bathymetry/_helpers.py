"""Pure, stateless helpers for the bathymetry backend.

No SDK and no network: these build the ERDDAP `griddap` subset URL the
backend GETs, so they are unit-testable in isolation. The exact URL shape
(`…/griddap/<id>.nc?<var>[(lat_lo):1:(lat_hi)][(lon_lo):1:(lon_hi)]`, no
time axis — the DEMs are static) was pinned live in the A1 gate; see
`planning/bathymetry/captures/bathymetry-sdk-facts.md`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

#: Default sampling stride for a griddap axis range (`1` = full resolution).
_DEFAULT_STEP = 1

#: Parses a `"<value> arc-(second|minute)"` native-resolution label.
_RESOLUTION_RE = re.compile(r"\s*([\d.]+)\s*arc-(second|minute)", re.IGNORECASE)


def resolution_degrees(native_resolution: str) -> float | None:
    """Convert a `"<n> arc-second"` / `"arc-minute"` label to degrees.

    Args:
        native_resolution: A catalog row's `native_resolution` label
            (`"15 arc-second"`, `"1 arc-minute"`).

    Returns:
        float | None: The cell size in degrees, or `None` when the label
            is not a recognised arc-second / arc-minute string.

    Examples:
        - Arc-seconds and arc-minutes convert to degrees:
            ```python
            >>> from earthlens.bathymetry._helpers import resolution_degrees
            >>> round(resolution_degrees("15 arc-second"), 6)
            0.004167
            >>> resolution_degrees("1 arc-minute")
            0.016666666666666666
            >>> resolution_degrees("native") is None
            True

            ```
    """
    match = _RESOLUTION_RE.match(native_resolution or "")
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value / 3600.0 if unit == "second" else value / 60.0


def estimate_grid_pixels(
    bbox: tuple[float, float, float, float], native_resolution: str
) -> tuple[int, int] | None:
    """Estimate the `(width, height)` pixel dimensions of a bbox subset.

    Args:
        bbox: `(west, south, east, north)` in degrees.
        native_resolution: The DEM's `native_resolution` label.

    Returns:
        tuple[int, int] | None: `(width_px, height_px)`, each at least 1, or
            `None` when the resolution label is not parseable.
    """
    degrees = resolution_degrees(native_resolution)
    if degrees is None or degrees <= 0:
        return None
    west, south, east, north = bbox
    width = max(1, round(abs(east - west) / degrees))
    height = max(1, round(abs(north - south) / degrees))
    return width, height


def _normalise_lon(lon: float, lon_convention: str) -> float:
    """Map a `-180..180` longitude onto the server's convention.

    Args:
        lon: A longitude in the user's `-180..180` frame.
        lon_convention: The server's frame — `"-180..180"` (pass through)
            or `"0..360"` (wrap negatives, e.g. `-18 -> 342`).

    Returns:
        float: The longitude in the server's frame.
    """
    if lon_convention == "0..360":
        return lon % 360.0
    return float(lon)


def bbox_from_extent(space: SpatialExtent) -> tuple[float, float, float, float]:
    """Return the `(west, south, east, north)` bbox of a spatial extent.

    Args:
        space: A :class:`~earthlens.base.SpatialExtent` (the backend's
            `self.space`).

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)` in
            degrees.
    """
    return (space.west, space.south, space.east, space.north)


def griddap_subset_url(
    endpoint: str,
    dataset_id: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    lon_convention: str = "-180..180",
    step: int = _DEFAULT_STEP,
) -> str:
    """Build the ERDDAP `griddap` `.nc` subset URL for a static DEM bbox.

    The DEMs have no time axis, so the URL carries exactly two coordinate
    ranges — latitude then longitude, matching the grid's `[latitude]
    [longitude]` dimension order. The request bbox (`-180..180`) is
    normalised to the server's `lon_convention` first.

    Args:
        endpoint: ERDDAP base URL (a trailing slash is tolerated).
        dataset_id: The griddap coverage id on that server.
        variable: The elevation band name (`"elevation"` / `"z"`).
        bbox: `(west, south, east, north)` in `-180..180` degrees.
        lon_convention: The server's longitude frame — `"-180..180"` or
            `"0..360"`.
        step: Sampling stride per axis (`1` = native resolution).

    Returns:
        str: The full `…/griddap/<id>.nc?<var>[(s):step:(n)][(w):step:(e)]`
            download URL.

    Raises:
        ValueError: If, after normalisation, the western longitude exceeds
            the eastern one (an antimeridian-crossing bbox the single-URL
            form cannot express — split it into two requests).

    Examples:
        - A `-180..180` row passes the bbox straight through:
            ```python
            >>> from earthlens.bathymetry._helpers import griddap_subset_url
            >>> griddap_subset_url(
            ...     "https://coastwatch.pfeg.noaa.gov/erddap",
            ...     "GEBCO_2020",
            ...     "elevation",
            ...     (-18.0, 25.0, -17.0, 26.0),
            ... )
            'https://coastwatch.pfeg.noaa.gov/erddap/griddap/GEBCO_2020.nc?elevation[(25.0):1:(26.0)][(-18.0):1:(-17.0)]'

            ```
        - A `0..360` row wraps negative longitudes:
            ```python
            >>> griddap_subset_url(
            ...     "https://example.org/erddap",
            ...     "DEM360",
            ...     "z",
            ...     (-18.0, 25.0, -17.0, 26.0),
            ...     lon_convention="0..360",
            ... )
            'https://example.org/erddap/griddap/DEM360.nc?z[(25.0):1:(26.0)][(342.0):1:(343.0)]'

            ```
    """
    west, south, east, north = bbox
    west_n = _normalise_lon(west, lon_convention)
    east_n = _normalise_lon(east, lon_convention)
    if west_n > east_n:
        raise ValueError(
            f"bbox is inverted or crosses the antimeridian in the server's "
            f"{lon_convention!r} frame (west {west_n} > east {east_n}): pass "
            "west < east for a contiguous box, or split an "
            "antimeridian-crossing request into two."
        )
    base = f"{endpoint.rstrip('/')}/griddap/{dataset_id}.nc?"
    lat_range = f"[({south}):{step}:({north})]"
    lon_range = f"[({west_n}):{step}:({east_n})]"
    return f"{base}{variable}{lat_range}{lon_range}"
