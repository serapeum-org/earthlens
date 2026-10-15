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

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = [
    "Aspect",
    "Hillshade",
    "Slope",
]


def _like(ds: Dataset, array) -> Dataset:  # noqa: ANN001 - array is numpy or dask
    """Put `array` back on `ds`' grid as a new raster."""
    from pyramids.dataset import Dataset as PyramidsDataset

    return PyramidsDataset.dataset_like(ds, array)


class Slope(RasterOperation):
    """Steepness of an elevation band, in degrees or percent.

    Examples:
        - Slope of a flat surface is zero everywhere:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> flat = Dataset.from_array(
            ...     arr=np.full((4, 4), 100.0, dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ... )
            >>> float(np.asarray(Slope()(flat).read_array()).max())
            0.0

            ```
    """

    band: int = 0
    units: Literal["degrees", "percent"] = "degrees"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band slope raster.

        Args:
            ds: An elevation raster.

        Returns:
            A new raster of slope values in the configured units.
        """
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
