"""`RandomSampler` / `StratifiedSampler`: generate labelled training points.

Both draw points inside a bounding region and return an `ee.FeatureCollection`
of sampled pixels (with `geometries=True`, so each point keeps its coordinates
for a downstream classifier):

* `RandomSampler` draws `n_points` uniformly at random over the region via
  `ee.Image.sample`.
* `StratifiedSampler` draws per-class counts from a class/label band via
  `ee.Image.stratifiedSample` - a scalar `n_per_class` goes through `numPoints`,
  while a `{class_value: count}` mapping is split into parallel
  `classValues` / `classPoints` lists so each class's count is honoured exactly.

The `geometry` argument is the bounding region (a polygon), not the points
themselves; its first geometry is converted to an `ee.Geometry` by the shared
`GeometryConverter`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.vector.geometry import GeometryConverter

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from geopandas.geodataframe import GeoDataFrame

__all__ = ["RandomSampler", "StratifiedSampler"]


class RandomSampler(Sampler):
    """Draw `n_points` random samples over a region.

    Args:
        scale_m: The pixel scale (metres) at which to sample.
        n_points: How many pixels to draw.
        seed: The random seed for reproducibility, or `None`.

    Examples:
        - A random sampler carries its point budget and seed:
            ```python
            >>> from earthlens.gee.sampling.random import RandomSampler
            >>> sampler = RandomSampler(scale_m=30, n_points=500, seed=42)
            >>> (sampler.n_points, sampler.seed)
            (500, 42)

            ```
    """

    _REGION_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset({"Polygon", "MultiPolygon"})

    n_points: int
    seed: int | None = None

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Draw random points over the region and read `image` at each.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` whose first geometry bounds the draw.

        Returns:
            An `ee.FeatureCollection` of the sampled points, each carrying the
            band value(s) and its coordinates.

        Raises:
            ValueError: If `geometry` is empty or not a polygon region.
        """
        self._validate_geometry(geometry, self._REGION_GEOM_TYPES)
        region = GeometryConverter().to_ee_geometry(geometry.geometry.iloc[0])
        collection = image.sample(
            region=region,
            scale=self.scale_m,
            numPixels=self.n_points,
            seed=self.seed,
            geometries=True,
        )
        return cast("ee.FeatureCollection", collection)


class StratifiedSampler(Sampler):
    """Draw per-class training points from a class/label band.

    Args:
        scale_m: The pixel scale (metres) at which to sample.
        class_band: The band naming each pixel's class.
        n_per_class: A scalar count applied to every class, or a
            `{class_value: count}` mapping for exact per-class counts.
        class_values: The class values to sample when `n_per_class` is a scalar;
            `None` samples every class present.
        seed: The random seed for reproducibility, or `None`.

    Examples:
        - A mapping gives each class its own count:
            ```python
            >>> from earthlens.gee.sampling.random import StratifiedSampler
            >>> sampler = StratifiedSampler(
            ...     scale_m=30, class_band="landcover", n_per_class={1: 100, 2: 50}
            ... )
            >>> sampler.n_per_class
            {1: 100, 2: 50}

            ```
    """

    _REGION_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset({"Polygon", "MultiPolygon"})

    class_band: str
    n_per_class: int | dict[int, int]
    class_values: tuple[int, ...] | None = None
    seed: int | None = None

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Draw per-class points over the region and read `image` at each.

        A scalar `n_per_class` is passed as `numPoints`; a mapping is split into
        parallel `classValues` / `classPoints` so each class's count is exact.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` whose first geometry bounds the draw.

        Returns:
            An `ee.FeatureCollection` of the sampled points, each carrying the
            band value(s) and its coordinates.

        Raises:
            ValueError: If `geometry` is empty or not a polygon region.
        """
        self._validate_geometry(geometry, self._REGION_GEOM_TYPES)
        region = GeometryConverter().to_ee_geometry(geometry.geometry.iloc[0])
        if isinstance(self.n_per_class, dict):
            keys = list(self.n_per_class)
            class_values: list[int] | None = keys
            class_points: list[int] | None = [
                self.n_per_class[value] for value in keys
            ]
            num_points = 0
        else:
            class_values = (
                list(self.class_values) if self.class_values is not None else None
            )
            class_points = None
            num_points = self.n_per_class
        collection = image.stratifiedSample(
            numPoints=num_points,
            classBand=self.class_band,
            region=region,
            scale=self.scale_m,
            classValues=class_values,
            classPoints=class_points,
            seed=self.seed,
            geometries=True,
        )
        return cast("ee.FeatureCollection", collection)
