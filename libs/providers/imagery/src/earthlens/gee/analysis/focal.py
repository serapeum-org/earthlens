"""Neighbourhood (focal) statistics over a square window.

`FocalMean`, `FocalStd` and `FocalMedian` are the named reducers; `FocalApply`
takes any callable, which is what makes texture measures and custom kernels
possible without a new operation per formula.

One thing the window reducers have to get right: pyramids hands the callable a
flat window with no-data cells as `NaN`, and whatever the callable returns for
such a window is written back as the band's sentinel. A NaN-blind reducer
therefore blanks every window that merely touches a void - `np.max` returns
`NaN` and loses the whole neighbourhood where `np.nanmax` ignores the void. The
named operations here use the `np.nan*` family for exactly that reason, and
`FocalApply` says so in its docstring because it cannot enforce it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import ConfigDict, field_validator

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = [
    "FocalApply",
    "FocalMean",
    "FocalMedian",
    "FocalStd",
]


def _focal(
    ds: Dataset, func: Callable[[Any], float], radius: int, band: int
) -> Dataset:
    """Run `func` over every window and put the result back on `ds`' grid."""
    from pyramids.dataset import Dataset as PyramidsDataset

    return PyramidsDataset.dataset_like(
        ds, ds.focal_apply(func, radius=radius, band=band)
    )


class _Window(RasterOperation):
    """Shared configuration for the window reducers."""

    radius: int = 1
    band: int = 0

    @field_validator("radius")
    @classmethod
    def _positive_radius(cls, value: int) -> int:
        """Reject a radius that would make the window empty or ill-defined."""
        if value < 1:
            raise ValueError(
                f"radius must be at least 1 (a 3x3 window), got {value}; "
                "radius 0 would reduce each cell to itself"
            )
        return value


class FocalMean(_Window):
    """Mean of each `(2*radius+1)` square window, ignoring no-data cells.

    Examples:
        - Smoothing a constant surface changes nothing:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> flat = Dataset.from_array(
            ...     arr=np.full((5, 5), 7.0, dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ... )
            >>> float(np.asarray(FocalMean()(flat).read_array())[2, 2])
            7.0

            ```
    """

    def apply(self, ds: Dataset) -> Dataset:
        """Return the window-mean raster.

        Args:
            ds: The source raster.

        Returns:
            A new raster of window means.
        """
        return _focal(ds, np.nanmean, self.radius, self.band)


class FocalStd(_Window):
    """Standard deviation of each square window, ignoring no-data cells."""

    def apply(self, ds: Dataset) -> Dataset:
        """Return the window standard-deviation raster.

        Args:
            ds: The source raster.

        Returns:
            A new raster of window standard deviations.
        """
        return _focal(ds, np.nanstd, self.radius, self.band)


class FocalMedian(_Window):
    """Median of each square window, ignoring no-data cells."""

    def apply(self, ds: Dataset) -> Dataset:
        """Return the window-median raster.

        Args:
            ds: The source raster.

        Returns:
            A new raster of window medians.
        """
        return _focal(ds, np.nanmedian, self.radius, self.band)


class FocalApply(_Window):
    """Any callable reduced over each square window.

    The callable receives the flat window in physical units, with no-data cells
    as `NaN`, and returns one number. It must be NaN-aware: a reducer that
    propagates `NaN` blanks every window touching a void, which pyramids then
    writes back as the band's no-data value.

    `arbitrary_types_allowed` is on because the field holds a function, which
    pydantic has no schema for; the operation stays frozen, so the callable
    cannot be swapped after construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    func: Callable[[Any], float]

    @property
    def name(self) -> str:
        """The operation plus the callable's name, so a chain says what it ran."""
        label = getattr(self.func, "__name__", type(self.func).__name__)
        return f"FocalApply({label})"

    def apply(self, ds: Dataset) -> Dataset:
        """Return the raster of per-window results.

        Args:
            ds: The source raster.

        Returns:
            A new raster holding one value per window.
        """
        return _focal(ds, self.func, self.radius, self.band)
