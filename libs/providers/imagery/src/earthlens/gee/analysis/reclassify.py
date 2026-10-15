"""Remap values into classes, and convert a band's physical units.

Two operations that both rewrite values rather than deriving new ones.
`Reclassify` bins continuous values into classes - the land-cover, slope-class
and risk-band case - and `ConvertUnits` delegates to pyramids' conversion table,
which knows what a unit label means.

`Reclassify` is deliberately explicit about what happens to a value no bin
covers: it becomes no-data, not silently the nearest class. A reclassification
that quietly invents a class for an unmatched value is a worse outcome than one
that leaves a hole a reader can see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from pydantic import field_validator, model_validator

from earthlens.gee.analysis._arrays import read_masked, result_like, validate_no_data
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["ConvertUnits", "Reclassify"]


class Reclassify(RasterOperation):
    """Bin a band's values into classes by half-open ranges.

    Each bin is `(lower, upper, class_value)` and matches `lower <= v < upper`,
    so adjacent bins can share an edge without overlapping or leaving a gap.
    Use `-inf` / `inf` for an open end.

    Examples:
        - Three slope classes from a ramp:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> raster = Dataset.from_array(
            ...     arr=np.array([[0.0, 5.0, 15.0, 40.0]], dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-9999.0,
            ... )
            >>> flat_to_steep = Reclassify(
            ...     bins=((0.0, 10.0, 1), (10.0, 30.0, 2), (30.0, float("inf"), 3)),
            ... )
            >>> np.asarray(flat_to_steep(raster).read_array()).tolist()
            [[1.0, 1.0, 2.0, 3.0]]

            ```
        - A value no bin covers becomes no-data rather than a guessed class:
            ```python
            >>> only_high = Reclassify(bins=((30.0, float("inf"), 9),))
            >>> out = np.asarray(only_high(raster).read_array())
            >>> float(out[0, 3]), bool(np.isnan(out[0, 0]))
            (9.0, True)

            ```
    """

    bins: tuple[tuple[float, float, float], ...]
    band: int = 0
    dtype: str = "float32"
    no_data: float | int | None = None

    @field_validator("bins")
    @classmethod
    def _non_empty_and_ordered(
        cls, value: tuple[tuple[float, float, float], ...]
    ) -> tuple[tuple[float, float, float], ...]:
        """Reject an empty bin list and any bin whose bounds are inverted.

        Args:
            value: The configured bins.

        Returns:
            The bins unchanged once they are usable.

        Raises:
            ValueError: If there are no bins, or a bin's lower bound is not
                below its upper bound - which would match nothing and is
                always a mistake rather than an intent.
        """
        if not value:
            raise ValueError("bins is empty, so every value would become no-data")
        for lower, upper, class_value in value:
            if not lower < upper:
                raise ValueError(
                    f"bin ({lower}, {upper}, {class_value}) matches nothing: "
                    "the lower bound must be below the upper one"
                )
        return value

    @model_validator(mode="after")
    def _sentinel_fits_the_dtype(self) -> Reclassify:
        """Refuse a sentinel the output dtype cannot store, at construction.

        Returns:
            The operation, once its sentinel is representable.

        Raises:
            ValueError: If `no_data` cannot be stored in `dtype` - NaN in an
                integer band is cast to 0, which collides with a real class, so
                it has to fail here rather than silently at apply time.
        """
        validate_no_data(self.no_data, self.dtype)
        return self

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band raster of class values.

        Args:
            ds: The source raster.

        Returns:
            A new single-band raster holding one class value per cell, with
            unmatched cells as no-data.
        """
        # Masked, so an open lower bin cannot sweep the sentinel into the
        # lowest class - `(-inf, 10]` otherwise matches -9999.
        values = read_masked(ds, self.band)
        classified = np.full(values.shape, np.nan, dtype="float64")
        for lower, upper, class_value in self.bins:
            inside = (values >= lower) & (values < upper)
            classified = np.where(inside, class_value, classified)
        return result_like(ds, classified, dtype=self.dtype, no_data=self.no_data)


class ConvertUnits(RasterOperation):
    """Convert a band's values to another physical unit.

    The conversion table lives in pyramids, which also records the new unit on
    the result - so this operation carries no unit arithmetic of its own. An
    unknown or incompatible unit label is pyramids' error to raise, not one this
    wrapper second-guesses.
    """

    target: str
    band: int | None = None

    @property
    def name(self) -> str:
        """The operation and the unit asked for, for a chain's name."""
        return f"ConvertUnits({self.target})"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster whose values are in `target` units.

        Args:
            ds: The source raster, whose bands declare their current units.

        Returns:
            A new raster in the requested unit.
        """
        return ds.convert_units(self.target, band=self.band)
