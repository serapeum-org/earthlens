"""Terrain derivatives of an elevation raster: slope, aspect and hillshade.

Each operation is a thin wrapper over the matching pyramids method, which
returns a bare array; the wrapper puts it back on the source grid with
`Dataset.dataset_like` so the result is a georeferenced raster like every other
operation's output.

Nothing here reimplements the arithmetic. pyramids computes these with
cartographic defaults, and a second implementation would be a second set of
edge-handling bugs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = [
    "Aspect",
    "Hillshade",
    "Slope",
]


def _like(ds: Dataset, array: np.ndarray) -> Dataset:
    """Put `array` back on `ds`' grid as a new raster.

    Annotated `np.ndarray` rather than left open: `Dataset.dataset_like` opens
    with `if not isinstance(array, np.ndarray): raise TypeError`, and none of
    these operations passes `chunks=`, so the value is always numpy. The
    previous `noqa` comment claimed a dask array was possible, which it is not.

    Args:
        ds: The raster whose grid the result sits on.
        array: The derivative values.

    Returns:
        A new georeferenced raster carrying `array`.
    """
    from pyramids.dataset import Dataset as PyramidsDataset

    return PyramidsDataset.dataset_like(ds, array)


class Slope(RasterOperation):
    """Steepness of an elevation band, in degrees, radians or percent.

    `degrees` and `radians` are pyramids' own; `percent` - rise over run times
    one hundred, the form a highway or drainage threshold is usually written in
    - is computed here as `tan(radians) * 100`, because pyramids does not offer
    it. That matters: pyramids falls through to **radians** for any unit string
    it does not recognise, so passing `"percent"` straight through returned
    0.785 where the answer is 100.

    Examples:
        - One metre of rise per metre of run, read in all three units:
            ```python
            >>> import math
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> metric = GeoReference(top_left_corner=(0.0, 0.0), cell_size=1.0, epsg=3857)
            >>> ramp = Dataset.from_array(
            ...     arr=np.tile(np.arange(5, dtype="float32"), (5, 1)), geo_ref=metric
            ... )
            >>> def centre(op):
            ...     return float(np.asarray(op(ramp).read_array())[2, 2])
            >>> round(centre(Slope()), 6)
            45.0
            >>> round(centre(Slope(units="radians")), 6) == round(math.pi / 4, 6)
            True
            >>> round(centre(Slope(units="percent")), 6)
            100.0

            ```
        - Slope of a flat surface is zero in every unit:
            ```python
            >>> flat = Dataset.from_array(
            ...     arr=np.full((4, 4), 100.0, dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ... )
            >>> float(np.asarray(Slope()(flat).read_array()).max())
            0.0

            ```
    """

    band: int = 0
    units: Literal["degrees", "radians", "percent"] = "degrees"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band slope raster.

        Args:
            ds: An elevation raster.

        Returns:
            A new raster of slope values in the configured units.
        """
        if self.units == "percent":
            radians = np.asarray(ds.slope(band=self.band, units="radians"))
            return _like(ds, np.tan(radians) * 100.0)
        return _like(ds, ds.slope(band=self.band, units=self.units))


class Aspect(RasterOperation):
    """Downslope compass direction of an elevation band, in degrees."""

    band: int = 0

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band aspect raster.

        Args:
            ds: An elevation raster.

        Returns:
            A new raster of aspect values, clockwise from north.
        """
        return _like(ds, ds.aspect(band=self.band))


class Hillshade(RasterOperation):
    """Shaded relief of an elevation band for a given sun position.

    The defaults are the cartographic convention - a light source in the
    north-west at 45 degrees - which is what makes a hillshade read as terrain
    rather than as an inverted embossing.
    """

    band: int = 0
    azimuth: float = 315.0
    altitude: float = 45.0

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band hillshade raster.

        Args:
            ds: An elevation raster.

        Returns:
            A new raster of shaded-relief values.
        """
        return _like(
            ds,
            ds.hillshade(band=self.band, azimuth=self.azimuth, altitude=self.altitude),
        )
