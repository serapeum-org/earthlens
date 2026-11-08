"""`PointSampler`: sample a raster at point locations via `reduceRegions`.

`PointSampler` is the `Sampler` form of the original `sample_points`: it inserts
the points into an `rtree` index, groups them into that index's leaves, and
issues one `image.clip(bbox).reduceRegions(...)` per leaf before merging the
results into one `ee.FeatureCollection`. This keeps the number of round-trips
bounded by the leaf count (not the point count) while keeping each clip box
tight. The leaf-batching sequence is preserved exactly; only the geometry
conversion (`GeometryConverter`) and the reducer (`self.reducer`) are the class's
own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import ee
from rtree import index

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.vector.geometry import GeometryConverter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.feature import FeatureCollection

__all__ = ["PointSampler"]


class PointSampler(Sampler):
    """Sample an `ee.Image` at every point of a `GeoDataFrame`.

    Examples:
        - A point sampler carries its scale and resolved reducer:
            ```python
            >>> from earthlens.gee.sampling.point import PointSampler
            >>> sampler = PointSampler(scale_m=30, reducer="mean")
            >>> (sampler.scale_m, sampler.reducer.name)
            (30.0, 'mean')

            ```
    """

    _POINT_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset({"Point", "MultiPoint"})

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Reduce `image` at each point, batching by `rtree` leaf.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` of `Point` / `MultiPoint` geometries; its
                non-geometry columns are carried through as feature properties.

        Returns:
            An `ee.FeatureCollection` of the input points, each carrying the
            reduced band value(s).

        Raises:
            ValueError: If `geometry` is empty or holds a non-point geometry.
        """
        self._validate_geometry(geometry, self._POINT_GEOM_TYPES)
        converter = GeometryConverter()
        rtree_index = index.Index()
        for row_index, geom in enumerate(geometry.geometry):
            rtree_index.insert(row_index, geom.bounds)
        collection = ee.FeatureCollection([])
        for _, indices, bbox in sorted(rtree_index.leaves()):
            ee_bbox = ee.Geometry.BBox(bbox[0], bbox[1], bbox[2], bbox[3])
            ee_feature_collection = converter.to_ee(geometry.iloc[indices])
            clipped = image.clip(ee_bbox)
            collection = collection.merge(
                ee.FeatureCollection(
                    clipped.reduceRegions(
                        collection=ee_feature_collection,
                        reducer=self.reducer.as_ee_reducer(),
                        scale=self.scale_m,
                    )
                )
            )
        return cast("ee.FeatureCollection", collection)

    def sample_to_gdf(
        self, image: ee.Image, geometry: GeoDataFrame, *, crs: int | str = 4326
    ) -> FeatureCollection:
        """Sample, then pull the result into a pyramids `FeatureCollection`.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` of point geometries.
            crs: CRS for the returned collection. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection` (a `GeoDataFrame`) of the sampled
            points, suitable for small-to-medium point sets (the `getInfo`
            path's ~5000-feature / ~10 MB cap).
        """
        from earthlens.gee.vector.download import FeatureDownloader

        return FeatureDownloader().to_gdf(self.sample(image, geometry), crs=crs)
