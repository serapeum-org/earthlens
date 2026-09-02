"""Backend-agnostic array/NetCDF-variable → pyramids raster helpers.

The provider backends that download a NetCDF and re-emit it as a GeoTIFF share
one step: rebuild an in-memory `(array, geotransform, epsg)` as a
`pyramids.Dataset` (NetCDF cubes do not expose a source SRS the crop warp can
read, so the variable is read out and re-tagged), optionally wrapping a
whole-globe 0-360 longitude grid to -180..180. Kept here so the s3 whole-
variable path and the cmems per-window slices build the raster the same way.
"""

from __future__ import annotations

from typing import Any


def close_quietly(handle: Any) -> None:
    """Release a pyramids handle, ignoring any error from the close itself.

    The shared form of the "best-effort handle release" the raster backends each
    re-spelled. Releasing the handle matters on Windows, where an open GDAL /
    netCDF handle blocks a later `open` or `unlink` of the same file, so an
    intermediate cannot be replaced or removed while it is held. The close is
    best-effort by design: it runs on the cleanup path of an operation that has
    already produced its result, so a failure to close must not mask that result
    or the exception being propagated.

    Accepts anything — a `Dataset`, `NetCDF`, `LabeledDataset`, `None`, or an
    object with no `close` at all — so a caller never has to guard the call.

    Args:
        handle: The object to close. Ignored when it is `None` or exposes no
            callable `close`.

    Examples:
        - A closeable handle is closed:
            ```python
            >>> from earthlens.base.raster import close_quietly
            >>> class Handle:
            ...     closed = False
            ...     def close(self):
            ...         self.closed = True
            >>> handle = Handle()
            >>> close_quietly(handle)
            >>> handle.closed
            True

            ```
        - A handle whose close fails is swallowed, not propagated:
            ```python
            >>> from earthlens.base.raster import close_quietly
            >>> class Stubborn:
            ...     def close(self):
            ...         raise OSError("still locked")
            >>> close_quietly(Stubborn())

            ```
        - `None` and objects without `close` are no-ops:
            ```python
            >>> from earthlens.base.raster import close_quietly
            >>> close_quietly(None)
            >>> close_quietly(object())

            ```
    """
    # The attribute lookup is inside the `try` on purpose: `close` may be a
    # property or arrive through a `__getattr__` on a lazy proxy, and such a
    # descriptor can raise something other than AttributeError — which
    # `getattr(..., None)` would not absorb. Since this runs on a cleanup path
    # (often inside `finally:`), letting that escape would mask the real
    # exception, so the whole access is guarded.
    try:
        closer = getattr(handle, "close", None)
        if callable(closer):
            closer()
    except Exception:  # noqa: BLE001 - best-effort release  # nosec B110
        pass


def array_to_raster(
    arr: Any,
    geo: Any,
    *,
    epsg: Any,
    wrap_longitude: bool = False,
) -> Any:
    """Build a pyramids `Dataset` from an array + geotransform.

    Args:
        arr: The pixel array (`(bands, rows, cols)` or `(rows, cols)`).
        geo: The GDAL 6-tuple geotransform for `arr`.
        epsg: The EPSG code to tag the raster with.
        wrap_longitude: When `True`, roll a whole-globe 0-360 longitude grid to
            -180..180 via `Dataset.wrap_longitude` (which validates the global
            span and raises `ValueError` for a non-global grid).

    Returns:
        A new `pyramids.Dataset`.
    """
    from pyramids.dataset import Dataset, GeoReference

    dataset = Dataset.from_array(arr=arr, geo_ref=GeoReference(geo=geo, epsg=epsg))
    if wrap_longitude:
        dataset = dataset.wrap_longitude()
    return dataset


def netcdf_variable_to_raster(
    nc: Any,
    name: str,
    *,
    epsg: Any = None,
    wrap_longitude: bool = False,
) -> Any:
    """Read a NetCDF variable and rebuild it as a pyramids `Dataset`.

    Reads the variable's array + geotransform from an already-open
    `pyramids.netcdf.NetCDF` and rebuilds it as a `Dataset` — NetCDF cubes do
    not expose a source SRS the crop warp can read, so cropping the cube
    directly fails.

    Args:
        nc: An open `pyramids.netcdf.NetCDF`.
        name: The in-file variable name to read.
        epsg: The EPSG code to tag the raster with; the variable's own `epsg`
            is used when `None`.
        wrap_longitude: When `True`, wrap a whole-globe 0-360 longitude grid to
            -180..180 (see :func:`array_to_raster`).

    Returns:
        A new `pyramids.Dataset` for the variable.
    """
    import numpy as np

    cube = nc.get_variable(name)
    return array_to_raster(
        np.asarray(cube.read_array()),
        tuple(cube.geotransform),
        epsg=cube.epsg if epsg is None else epsg,
        wrap_longitude=wrap_longitude,
    )
