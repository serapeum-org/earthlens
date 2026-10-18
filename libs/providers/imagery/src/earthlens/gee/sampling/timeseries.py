"""`TimeSeriesSampler`: a per-point time-series across a stack of images.

`TimeSeriesSampler` samples one point set against every image of a time stack -
one `ee.Image` per time bucket - and returns a long table of one row per point
per bucket (or a wide table, one column per timestamp, with `wide=True`). Each
bucket's image is reduced through a shared `PointSampler`, and every resulting
feature is stamped with that bucket's `system:time_start` as a `timestamp`
property before the buckets are merged, so the series stays addressable by time
after the merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import ee

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.sampling.point import PointSampler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.feature import FeatureCollection

__all__ = ["TimeSeriesSampler"]


class TimeSeriesSampler(Sampler):
    """Sample a point set across every image of a time stack.

    Examples:
        - A time-series sampler defaults to the long table shape:
            ```python
            >>> from earthlens.gee.sampling.timeseries import TimeSeriesSampler
            >>> sampler = TimeSeriesSampler(scale_m=30)
            >>> sampler.wide
            False

            ```
    """

    wide: bool = False

    def sample(self, image: Any, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Reduce the points against each per-bucket image, stamped by timestamp.

        Args:
            image: An iterable of per-bucket `ee.Image`s (one per time bucket,
                e.g. the images of an `ee.ImageCollection`); each is read for its
                `system:time_start` timestamp.
            geometry: A `GeoDataFrame` of point geometries.

        Returns:
            One long `ee.FeatureCollection` of (points x buckets), every feature
            carrying a `timestamp` property.
        """
        point_sampler = PointSampler(scale_m=self.scale_m, reducer=self.reducer)
        merged = ee.FeatureCollection([])
        for bucket_image in image:
            timestamp = bucket_image.get("system:time_start")
            stamped = point_sampler.sample(bucket_image, geometry).map(
                lambda feature, ts=timestamp: feature.set("timestamp", ts)
            )
            merged = merged.merge(stamped)
        return cast("ee.FeatureCollection", merged)

    def sample_to_gdf(
        self, image: Any, geometry: GeoDataFrame, *, crs: int | str = 4326
    ) -> FeatureCollection:
        """Pull the series into a long (default) or wide (`wide=True`) table.

        Args:
            image: An iterable of per-bucket `ee.Image`s.
            geometry: A `GeoDataFrame` of point geometries.
            crs: CRS stamped on the pulled collection. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection`: one row per (point, timestamp) in long
            form, or one column per timestamp when `wide` is set.
        """
        from earthlens.gee.vector.download import FeatureDownloader

        long_table = FeatureDownloader().to_gdf(self.sample(image, geometry), crs=crs)
        result = (
            long_table.pivot_table(
                index="point_id", columns="timestamp", values="value"
            )
            if self.wide
            else long_table
        )
        return cast("FeatureCollection", result)
