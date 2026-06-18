"""Private helpers for the ASF backend.

Holds the small stateless helpers used by `backend.py`:

* :func:`wkt_from_extent` — convert a :class:`SpatialExtent` bbox
  to the WKT polygon string `asf_search.geo_search` expects via its
  `intersectsWith=` argument.
* :func:`apply_baseline_windows` — a defensive post-filter that
  drops stacked products whose `perpendicularBaseline` /
  `temporalBaseline` properties fall outside the requested windows.
  In practice the backend passes the windows as
  `ASFSearchOptions(minBaselinePerp, maxBaselinePerp,
  temporalBaselineDays)` and the SDK enforces them server-side, but
  this helper backs that up against partial-match bugs in older
  catalogs.
"""

from __future__ import annotations

from typing import Any

from shapely.geometry import box

from earthlens.base import SpatialExtent


def wkt_from_extent(space: SpatialExtent) -> str:
    """Convert a :class:`SpatialExtent` bbox to a WKT polygon.

    `asf_search.geo_search` accepts a WKT geometry as
    `intersectsWith=`. The bbox is taken as a flat WGS84 rectangle
    (`POLYGON((west south, east south, east north, west north, west
    south))`) — no antimeridian splitting; ASF accepts a single
    polygon and clips internally.

    Args:
        space: The backend's resolved :class:`SpatialExtent`. Must
            have `latitude_min <= latitude_max` and `longitude_min
            <= longitude_max` (the model validator enforces this).

    Returns:
        str: A WKT `POLYGON((...))` literal.

    Examples:
        - Round-trip a bbox through shapely:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.asf._helpers import wkt_from_extent
            >>> ext = SpatialExtent(latitude_min=0.0, latitude_max=1.0,
            ...                     longitude_min=2.0, longitude_max=3.0)
            >>> wkt_from_extent(ext).startswith("POLYGON")
            True

            ```
    """
    return box(
        space.longitude_min,
        space.latitude_min,
        space.longitude_max,
        space.latitude_max,
    ).wkt


def _in_window(value: float | int | None, window: tuple[float, float] | None) -> bool:
    """Return whether `value` lies inside the closed `window`.

    `window=None` is a wildcard (no filter applied); a `None`
    `value` against a non-`None` window fails (the SDK should never
    leave a stacked product without these properties, but the
    defensive check here keeps the filter total).

    Args:
        value: A baseline value pulled from
            `product.properties[<key>]`.
        window: A `(min, max)` tuple of inclusive bounds, or `None`
            to disable the filter.

    Returns:
        bool: `True` when the value is in the window or no window
            was requested.
    """
    if window is None:
        return True
    if value is None:
        return False
    return window[0] <= value <= window[1]


def apply_baseline_windows(
    products: list[Any],
    perpendicular_baseline: tuple[float, float] | None,
    temporal_baseline: tuple[int, int] | None,
) -> list[Any]:
    """Drop stacked products outside the requested baseline windows.

    Defensive post-filter — the backend passes the windows as real
    `ASFSearchOptions` so `asf_search` enforces them server-side
    already, but a server-side regression that lets edge values
    through would be silently wrong. This filter keeps the windows
    authoritative on the client too.

    Args:
        products: The stacked products (each an
            `asf_search.ASFProduct`), as returned by
            `ASFProduct.stack()`.
        perpendicular_baseline: `(min_m, max_m)` perpendicular
            baseline bounds in metres, or `None` to disable.
        temporal_baseline: `(min_days, max_days)` temporal baseline
            bounds in days, or `None` to disable.

    Returns:
        list[Any]: The subset of `products` whose baseline
            properties lie inside both windows. Ordering preserved.
    """
    return [
        product
        for product in products
        if _in_window(
            product.properties.get("perpendicularBaseline"), perpendicular_baseline
        )
        and _in_window(
            product.properties.get("temporalBaseline"), temporal_baseline
        )
    ]
