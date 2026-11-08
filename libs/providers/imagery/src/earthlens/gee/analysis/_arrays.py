"""The two array conversions every operation needs, in one place.

Both halves of the no-data contract live here, because getting either wrong is
silent corruption rather than a failure:

**Reading.** A no-data sentinel is a *number*. `-9999` is finite, so an
operation that filters with `np.isfinite` treats a void as data: one void six
cells from the centre was enough to change a texture measure from 6.5 to 0,
and `(-9999 - -9999) / (-9999 + -9999)` is `-0.0`, a perfectly plausible
index. `read_masked` reads through the band's declared sentinel and hands back
`NaN`, which arithmetic propagates and `np.nan*` reducers ignore.

**Writing.** `Dataset.dataset_like` copies the *source's* `no_data_value` onto
the result - but a derived raster holds new values in a new dtype, so the
source's sentinel is meaningless there. A reclassification writing `NaN` was
declared `-9999` and read back as data; cast to `uint8` the NaN became class
`0` and the sentinel wrapped to `241`. `result_like` builds the result with the
sentinel it actually uses, so a masked read sees the voids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["default_no_data", "read_masked", "result_like", "validate_no_data"]


def read_masked(ds: Dataset, band: int = 0) -> np.ndarray:
    """Return one band as `float64` with its no-data cells as `NaN`.

    Args:
        ds: The source raster.
        band: Zero-based band index.

    Returns:
        A plain `numpy.ndarray`, not a masked array: `NaN` travels through
        arithmetic and through `scipy`'s filters, where a mask does not.

    Raises:
        IndexError: If `band` is outside the raster.
    """
    if not 0 <= band < ds.band_count:
        raise IndexError(
            f"band {band} was requested of a raster with {ds.band_count} band(s)"
        )
    masked = ds.read_array(band=band, masked=True)
    return np.ma.filled(np.ma.masked_invalid(masked), np.nan).astype("float64")


def default_no_data(dtype: str) -> float | int:
    """Return the sentinel to use for `dtype` when the caller names none.

    Args:
        dtype: The result's numpy dtype name.

    Returns:
        `nan` for a floating dtype. For an integer dtype there is no
        unambiguous choice - every value is a possible class - so this raises
        instead of guessing.

    Raises:
        ValueError: If `dtype` is an integer type, since a sentinel then has to
            be chosen explicitly and reserved from the class values.
    """
    if np.issubdtype(np.dtype(dtype), np.floating):
        return float("nan")
    raise ValueError(
        f"dtype={dtype!r} cannot represent NaN, so no_data must be given "
        "explicitly and kept out of the value range (for example "
        "no_data=255 with dtype='uint8')"
    )


def validate_no_data(no_data: float | int | None, dtype: str) -> float | int:
    """Resolve and check the sentinel for a result of `dtype`.

    Args:
        no_data: The caller's sentinel, or `None` to take the default.
        dtype: The result's numpy dtype name.

    Returns:
        The sentinel to declare on the result.

    Raises:
        ValueError: If the sentinel cannot be stored in `dtype` - `NaN` in an
            integer band is the case that silently became class 0.
    """
    if no_data is None:
        return default_no_data(dtype)
    target = np.dtype(dtype)
    if (
        isinstance(no_data, float)
        and not np.isfinite(no_data)
        and not np.issubdtype(target, np.floating)
    ):
        raise ValueError(
            f"no_data={no_data!r} cannot be stored in dtype={dtype!r}; NaN in an "
            "integer band is cast to 0, which collides with a real class"
        )
    return no_data


def result_like(
    ds: Dataset,
    array: Any,
    *,
    no_data: float | int | None = None,
    dtype: str | None = None,
) -> Dataset:
    """Put `array` on `ds`' grid as a new raster declaring its own sentinel.

    The georeferencing is the source's - same transform, same CRS - but the
    no-data value is the one this result uses, not the one the input happened
    to carry.

    Args:
        ds: The raster whose grid and CRS the result adopts.
        array: The values, shaped like one band of `ds`.
        no_data: The sentinel to declare. `None` takes `nan` for a floating
            `dtype` and raises for an integer one.
        dtype: The result's dtype. `None` keeps `array`'s own.

    Returns:
        A new single-band `pyramids.dataset.Dataset`.
    """
    from pyramids.base.georeference import GeoReference
    from pyramids.dataset import Dataset as PyramidsDataset

    values = np.asarray(array)
    resolved_dtype = dtype or str(values.dtype)
    sentinel = validate_no_data(no_data, resolved_dtype)
    if not np.issubdtype(np.dtype(resolved_dtype), np.floating):
        # NaN has no integer representation, so voids take the sentinel before
        # the cast rather than becoming 0 during it.
        values = np.where(np.isnan(values), sentinel, values)
    return PyramidsDataset.from_array(
        arr=values.astype(resolved_dtype),
        geo_ref=GeoReference(geo=ds.geotransform, epsg=ds.epsg),
        no_data_value=sentinel,
    )
