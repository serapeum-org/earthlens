"""`TransectSampler`: sample a band along a line transect, server or client side.

`TransectSampler` reads raster values along a `LineString`, picking the mechanism
at the call site:

* `sample(image, geometry)` is the SERVER path: it densifies the line into
  `n_points` evenly spaced points (`numpy.linspace` over the line length feeding
  `shapely.geometry.LineString.interpolate`), carries each point's along-line
  distance in a `dist_m` column, and reduces them through `PointSampler` so the
  transect reuses the proven leaf-batched `reduceRegions` path. The unordered
  `reduceRegions` merge stays recoverable because every point keeps its `dist_m`.
* `sample_to_gdf(dataset, geometry)` is the CLIENT path: it calls pyramids'
  `Dataset.transect` (0.69.0) on an already-downloaded in-memory raster, which
  returns ordered points carrying `distance` + `value` columns directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import geopandas as gpd
import numpy as np

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.sampling.point import PointSampler

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.dataset import Dataset
    from pyramids.feature import FeatureCollection

__all__ = ["TransectSampler"]


class TransectSampler(Sampler):
    """Sample raster values along a `LineString` transect.

    Examples:
        - A transect sampler carries its point count and client-path band:
            ```python
            >>> from earthlens.gee.sampling.transect import TransectSampler
            >>> sampler = TransectSampler(scale_m=30, n_points=10)
            >>> (sampler.n_points, sampler.band)
            (10, 0)

            ```
    """

    _LINE_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"LineString", "MultiLineString"}
    )

    n_points: int = 100
    band: int = 0

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Densify the line into ordered points and reduce them in Earth Engine.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` whose first geometry is the
                `LineString` / `MultiLineString` transect.

        Returns:
            An `ee.FeatureCollection` of `n_points` points along the line, each
            carrying its along-line `dist_m` and the reduced band value(s).

        Raises:
            ValueError: If `geometry` is empty or not a line geometry.
        """
        self._validate_geometry(geometry, self._LINE_GEOM_TYPES)
        line = geometry.geometry.iloc[0]
        distances = np.linspace(0.0, line.length, self.n_points)
        points_gdf = gpd.GeoDataFrame(
            {
                "dist_m": distances,
                "geometry": [line.interpolate(distance) for distance in distances],
            },
            crs=geometry.crs,
        )
        sampler = PointSampler(scale_m=self.scale_m, reducer=self.reducer)
        return sampler.sample(image, points_gdf)

    def sample_to_gdf(
        self, dataset: Dataset, geometry: GeoDataFrame
    ) -> FeatureCollection:
        """Read the band along the line from an in-memory raster (client path).

        Args:
            dataset: An in-memory pyramids `Dataset` (e.g. a materialised
                download) to read along the line.
            geometry: A `GeoDataFrame` whose first geometry is the line.

        Returns:
            A pyramids `FeatureCollection` of ordered points carrying `distance`
            and `value` columns, from pyramids' `Dataset.transect`.
        """
        line = geometry.geometry.iloc[0]
        return cast(
            "FeatureCollection",
            dataset.transect(line, n=self.n_points, band=self.band),
        )
