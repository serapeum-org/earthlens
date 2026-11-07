"""`TimeSeriesSampler`: a per-point time-series across a stack of images.

`TimeSeriesSampler` samples one point set against every image of a time stack -
one `ee.Image` per time bucket - and returns a long table of one row per point
per bucket (or a wide table, one column per timestamp, with `wide=True`). Each
bucket's image is reduced through a shared `PointSampler`; every point carries a
stable `point_id` (its position in the input frame) and every resulting feature is
stamped with that bucket's `system:time_start` as a `timestamp` property before the
buckets are merged, so the series stays addressable by point and time after the
merge (which is what the `wide=True` table pivots on).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

import ee

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.sampling.point import PointSampler
from earthlens.gee.sampling.region import RegionSampler

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.feature import FeatureCollection

__all__ = ["RegionTimeSeriesSampler", "TimeSeriesSampler"]


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

    #: A time-series collapses each point to a row per timestamp, so the GEE
    #: backend reports `"tabular"` rather than `"vector"` for this sampler.
    output_kind: ClassVar[str] = "tabular"

    #: A time-series consumes the whole stack of per-bucket images at once (it
    #: reads each bucket's `system:time_start`), so the backend hands `sample`
    #: the full list of composited images rather than mapping it per bucket.
    consumes_stack: ClassVar[bool] = True

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
            carrying a `point_id` (its row position in `geometry`) and a `timestamp`
            property.
        """
        point_sampler = PointSampler(scale_m=self.scale_m, reducer=self.reducer)
        indexed = geometry.assign(point_id=list(range(len(geometry))))
        merged = ee.FeatureCollection([])
        for bucket_image in image:
            timestamp = bucket_image.get("system:time_start")
            stamped = point_sampler.sample(bucket_image, indexed).map(
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
        return cast("FeatureCollection", self.backend_to_gdf(image, geometry, crs=crs))

    def _reshape(self, gdf: GeoDataFrame) -> GeoDataFrame:
        """Pivot the long series to one column per timestamp when `wide`.

        Args:
            gdf: The downloaded long table (one row per point per bucket), each
                row carrying `point_id` and `timestamp`.

        Returns:
            The long table unchanged, or the wide pivot (one column per
            timestamp per value column) when `wide` is set.
        """
        if self.wide:
            value_columns = [
                column
                for column in gdf.columns
                if column not in ("point_id", "timestamp", "geometry")
            ]
            result = gdf.pivot_table(
                index="point_id", columns="timestamp", values=value_columns
            )
        else:
            result = gdf
        return cast("GeoDataFrame", result)


class RegionTimeSeriesSampler(Sampler):
    """Reduce every image of a time stack over polygon AOIs, per date.

    The polygon counterpart of `TimeSeriesSampler`: instead of sampling a point
    set, it reduces each per-bucket image over the input polygons with
    `reduceRegions` (a `RegionSampler` per bucket) and stamps every feature with
    that bucket's `system:time_start` as a `timestamp` property. This is the
    catchment / basin time series — "mean rainfall per catchment per month",
    "mean NDVI per field over the season" — reduced server-side and returned as
    a long table (one row per region per date), or a wide pivot with
    `wide=True`. Every region carries a stable `region_id` (its row position in
    the input frame), so the series stays addressable after the merge.

    Examples:
        - A region time-series sampler defaults to the mean and the long shape:
            ```python
            >>> from earthlens.gee.sampling.timeseries import RegionTimeSeriesSampler
            >>> sampler = RegionTimeSeriesSampler(scale_m=5566)
            >>> (sampler.stats, sampler.wide)
            (('mean',), False)

            ```
    """

    #: One row per (region, timestamp), so the backend reports a table.
    output_kind: ClassVar[str] = "tabular"

    #: Reads each bucket's `system:time_start`, so it consumes the whole stack.
    consumes_stack: ClassVar[bool] = True

    #: Reducer name(s) applied per polygon per date; one value column per band
    #: per stat. Mirrors `RegionSampler.stats`.
    stats: tuple[str, ...] = ("mean",)

    wide: bool = False

    def sample(self, image: Any, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Reduce the polygons against each per-bucket image, stamped by timestamp.

        Args:
            image: An iterable of per-bucket `ee.Image`s (one per time bucket);
                each is read for its `system:time_start` timestamp.
            geometry: A `GeoDataFrame` of `Polygon` / `MultiPolygon` geometries.

        Returns:
            One long `ee.FeatureCollection` of (regions x buckets), every feature
            carrying a `region_id` (its row position in `geometry`) and a
            `timestamp` property.
        """
        region_sampler = RegionSampler(scale_m=self.scale_m, stats=self.stats)
        indexed = geometry.assign(region_id=list(range(len(geometry))))
        merged = ee.FeatureCollection([])
        for bucket_image in image:
            timestamp = bucket_image.get("system:time_start")
            stamped = region_sampler.sample(bucket_image, indexed).map(
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
            geometry: A `GeoDataFrame` of polygon geometries.
            crs: CRS stamped on the pulled collection. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection`: one row per (region, timestamp) in
            long form, or one column per timestamp when `wide` is set.
        """
        return cast("FeatureCollection", self.backend_to_gdf(image, geometry, crs=crs))

    def _reshape(self, gdf: GeoDataFrame) -> GeoDataFrame:
        """Pivot the long series to one column per timestamp when `wide`.

        Args:
            gdf: The downloaded long table (one row per region per bucket), each
                row carrying `region_id` and `timestamp`.

        Returns:
            The long table unchanged, or the wide pivot (one column per
            timestamp per value column) when `wide` is set.
        """
        if self.wide:
            value_columns = [
                column
                for column in gdf.columns
                if column not in ("region_id", "timestamp", "geometry")
            ]
            result = gdf.pivot_table(
                index="region_id", columns="timestamp", values=value_columns
            )
        else:
            result = gdf
        return cast("GeoDataFrame", result)
