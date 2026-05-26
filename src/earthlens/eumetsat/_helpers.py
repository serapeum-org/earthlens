"""Small, dependency-free helpers for the EUMETSAT backend.

Currently the bounding-box formatter that turns an earthlens
`SpatialExtent` into the comma-separated `W,S,E,N` string `eumdac`'s
OpenSearch `bbox=` parameter expects (the axis order is the documented
EUMDAC gotcha — confirmed `W,S,E,N` against `eumdac` 3.1.1), plus the
antimeridian-split helper that yields the bbox string(s) to search.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent


def eumdac_bbox(west: float, south: float, east: float, north: float) -> str:
    """Format a WGS84 bounding box as `eumdac`'s `W,S,E,N` query string.

    `eumdac`'s OpenSearch `bbox=` parameter is a comma-separated string
    in **west, south, east, north** order (verified against the EUMDAC
    CLI, whose `--bbox` metavar is `("W", "S", "E", "N")`).

    Args:
        west: Western edge (min longitude), degrees.
        south: Southern edge (min latitude), degrees.
        east: Eastern edge (max longitude), degrees.
        north: Northern edge (max latitude), degrees.

    Returns:
        str: The `"west,south,east,north"` string.

    Examples:
        - A small box around the prime meridian:
            ```python
            >>> from earthlens.eumetsat._helpers import eumdac_bbox
            >>> eumdac_bbox(-1.0, 50.0, 1.0, 52.0)
            '-1.0,50.0,1.0,52.0'

            ```
    """
    return f"{west},{south},{east},{north}"


def safe_product_filename(product_id: str) -> str:
    """Reduce a product id to a safe, traversal-free on-disk filename.

    `eumdac` product ids are normally filename-safe, but the value comes
    from the server, so it is treated as untrusted: any directory
    component is stripped (the basename is kept) and an empty or
    traversal-only id (`.` / `..`) is rejected. This prevents a product
    id containing a path separator from writing outside the output
    directory or escaping it.

    Args:
        product_id: The product id (`str(product)`), to be used as a
            filename.

    Returns:
        str: The sanitised basename, safe to join under the output dir.

    Raises:
        ValueError: When the id is empty or reduces to `.` / `..`.

    Examples:
        - A plain id is returned unchanged:
            ```python
            >>> from earthlens.eumetsat._helpers import safe_product_filename
            >>> safe_product_filename("MSG4-SEVI-MSG15-0100-NA-20240601.nat")
            'MSG4-SEVI-MSG15-0100-NA-20240601.nat'

            ```
        - Directory components are stripped to the basename:
            ```python
            >>> from earthlens.eumetsat._helpers import safe_product_filename
            >>> safe_product_filename("../../etc/passwd")
            'passwd'

            ```
    """
    name = product_id.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", ".."):
        raise ValueError(
            f"product id {product_id!r} does not yield a usable filename"
        )
    return name


def antimeridian_bboxes(space: SpatialExtent) -> list[str]:
    """Return the `eumdac` bbox string(s) covering a spatial extent.

    An earthlens `SpatialExtent` constrains longitude to `[-180, 180]`
    with `west <= east`, so it cannot itself represent a box that
    crosses the antimeridian — such a request is normally split before
    reaching here. This helper returns a single-element list for the
    representable case and is the seam where a future antimeridian split
    would return the two halves.

    Args:
        space: The validated request extent.

    Returns:
        list[str]: One `eumdac` bbox string per search box (one element
            for a standard, non-crossing extent).

    Examples:
        - A standard extent yields one bbox string:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.eumetsat._helpers import antimeridian_bboxes
            >>> space = SpatialExtent.from_pairs(lat_lim=[50, 52], lon_lim=[-1, 1])
            >>> antimeridian_bboxes(space)
            ['-1.0,50.0,1.0,52.0']

            ```
    """
    return [eumdac_bbox(space.west, space.south, space.east, space.north)]
