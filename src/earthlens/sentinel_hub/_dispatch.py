"""Request-plane selection for the Sentinel Hub backend.

Pure, network-free helpers that decide *which* request plane a download uses.
The backend passes the resolved render size (px/side) and whether a `geometry=`
was supplied; these functions return the plane name (one of
:data:`earthlens.sentinel_hub._helpers.VALID_APIS`). Kept separate from
`backend.py` so the routing rules are unit-testable in isolation.

The auto-routing rule (`G4` / `G5`), when `api=` is omitted:

* a `geometry=` request → `"statistical"` (zonal stats over the polygon);
* otherwise a raster render routed by size: `≤ SH_MAX_DIMENSION` → `"process"`,
  `≤ ASYNC_MAX_DIMENSION` → `"async"`, larger → `"batch"`.

An explicit `api=` is honoured verbatim (only validated), so a user can force
`"process"` on a request the auto-rule would route elsewhere — the size guard in
the backend then raises if that forced plane cannot satisfy the request.
"""

from __future__ import annotations

from earthlens.sentinel_hub._helpers import (
    ASYNC_MAX_DIMENSION,
    SH_MAX_DIMENSION,
    VALID_APIS,
)


def validate_api(api: str | None) -> None:
    """Validate an explicit `api=` value, if any.

    Args:
        api: The requested plane, or `None` for auto-selection.

    Raises:
        ValueError: When `api` is a non-`None` value outside :data:`VALID_APIS`.
    """
    if api is not None and api not in VALID_APIS:
        raise ValueError(
            f"unknown api={api!r}: choose one of {list(VALID_APIS)} or omit it "
            "for size-based auto-selection."
        )


def auto_select_api(max_side_px: int, has_geometry: bool) -> str:
    """Pick the plane for a request when `api=` was omitted.

    Args:
        max_side_px: The larger of the render's two pixel dimensions.
        has_geometry: Whether a `geometry=` (polygon / FeatureCollection) was
            supplied (selects the tabular Statistical plane).

    Returns:
        The selected plane name.

    Examples:
        - A geometry request goes to the Statistical plane:
            ```python
            >>> from earthlens.sentinel_hub._dispatch import auto_select_api
            >>> auto_select_api(512, has_geometry=True)
            'statistical'

            ```
        - A small raster goes to Process; an oversized one to Batch:
            ```python
            >>> from earthlens.sentinel_hub._dispatch import auto_select_api
            >>> auto_select_api(1024, has_geometry=False)
            'process'
            >>> auto_select_api(40000, has_geometry=False)
            'batch'

            ```
    """
    if has_geometry:
        return "statistical"
    if max_side_px <= SH_MAX_DIMENSION:
        return "process"
    if max_side_px <= ASYNC_MAX_DIMENSION:
        return "async"
    return "batch"


def resolve_api(api: str | None, max_side_px: int, has_geometry: bool) -> str:
    """Validate an explicit `api=`, or auto-select one by size / geometry.

    Args:
        api: The requested plane, or `None` for auto-selection.
        max_side_px: The larger render dimension in pixels.
        has_geometry: Whether a `geometry=` was supplied.

    Returns:
        The resolved plane name.

    Raises:
        ValueError: When `api` is a non-`None` value outside :data:`VALID_APIS`.
    """
    validate_api(api)
    if api is not None:
        return api
    return auto_select_api(max_side_px, has_geometry)
