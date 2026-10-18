"""Sample raster values over geometries via Google Earth Engine.

This package is the `Sampler` family — one `Sampler` subclass per sampling
pattern, each taking an `ee.Image` (or an in-memory pyramids `Dataset`) and a
`GeoDataFrame`, and returning an `ee.FeatureCollection` / pyramids
`FeatureCollection` whose features carry the sampled value(s):

* `PointSampler` — read the raster at point geometries.
* `RegionSampler` / `zonal_stats` — per-polygon zonal statistics, server-side
  (`reduceRegions`) or client-side (a local pyramids reduction).
* `RandomSampler` / `StratifiedSampler` — draw labelled training points over a
  region, uniformly or per class.
* `TransectSampler` — sample a band along a line transect.
* `TimeSeriesSampler` — sample one geometry across an image collection's time
  axis, long or wide.
* `ZonalHistogram` — per-polygon value histograms.

Each sampler resolves its reducer through the shared `earthlens.gee.reducers`
registry at construction, so a bad name fails closed with a readable error (no
`eval`); the point/region samplers additionally enforce that the reducer is one
a `sample`/`reduceRegions` path can run (see `Sampler.strict_reducers`).
`PointSampler` batches its work through an `rtree.index.Index`: the points are
grouped into the index's leaves and one `image.reduceRegions` call is issued per
leaf (clipped to that leaf's bounding box), so the number of round-trips tracks
the leaf count rather than the point count.

`sample_points` / `sample_points_to_gdf` are the original free-function entry
points for point sampling, kept for back-compat; they are now thin shims over
`PointSampler`, which owns the one copy of the leaf-batching loop. New code
should prefer `PointSampler`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from earthlens.gee.reducers import resolve_reducer
from earthlens.gee.sampling.base import Sampler
from earthlens.gee.sampling.histogram import ZonalHistogram
from earthlens.gee.sampling.point import PointSampler
from earthlens.gee.sampling.random import RandomSampler, StratifiedSampler
from earthlens.gee.sampling.region import RegionSampler, ZonalStrategy, zonal_stats
from earthlens.gee.sampling.timeseries import TimeSeriesSampler
from earthlens.gee.sampling.transect import TransectSampler

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.feature import FeatureCollection

__all__ = [
    "PointSampler",
    "RandomSampler",
    "RegionSampler",
    "Sampler",
    "StratifiedSampler",
    "TimeSeriesSampler",
    "TransectSampler",
    "ZonalHistogram",
    "ZonalStrategy",
    "sample_points",
    "sample_points_to_gdf",
    "zonal_stats",
]


def sample_points(
    image: ee.Image,
    gdf: GeoDataFrame,
    *,
    scale_m: float,
    reducer: str = "first",
) -> ee.FeatureCollection:
    """Sample an Earth Engine image at every point in a `GeoDataFrame`.

    A back-compat shim over `PointSampler`: builds `PointSampler(scale_m=...,
    reducer=...)` and returns its `sample(image, gdf)`. The reducer is validated
    at construction against the sampling set, so a non-sampling reducer (e.g.
    `mosaic`) is refused up front, exactly as before.

    Args:
        image: The `ee.Image` to sample.
        gdf: A `GeoDataFrame` whose geometries are points; non-geometry columns
            are carried through as feature properties.
        scale_m: The pixel scale (metres) passed to `reduceRegions(scale=...)`.
        reducer: A sampling reducer name (e.g. `"first"`, `"mean"`) or a
            `"percentile:<q>"` spec. Defaults to `"first"`.

    Returns:
        An `ee.FeatureCollection` of the input points, each carrying the reduced
        band value(s).

    Raises:
        ValueError: If `reducer` is not a valid sampling reducer, if `gdf` is
            empty, or if any geometry is not a `Point` / `MultiPoint`.
    """
    return PointSampler(scale_m=scale_m, reducer=resolve_reducer(reducer)).sample(
        image, gdf
    )


def sample_points_to_gdf(
    image: ee.Image,
    gdf: GeoDataFrame,
    *,
    scale_m: float,
    reducer: str = "first",
    crs: int | str = 4326,
) -> FeatureCollection:
    """Sample an Earth Engine image at points and return a `FeatureCollection`.

    A back-compat shim over `PointSampler.sample_to_gdf`: the leaf-batched
    `reduceRegions` work plus a single `getInfo()` reshape into a pyramids
    `FeatureCollection` (a `GeoDataFrame`). Suitable for small-to-medium point
    sets (the `getInfo()` ~5000-feature / ~10 MB cap).

    Args:
        image: The `ee.Image` to sample.
        gdf: A `GeoDataFrame` of point geometries.
        scale_m: The pixel scale (metres) passed to `reduceRegions(scale=...)`.
        reducer: A sampling reducer name or a `"percentile:<q>"` spec. Defaults
            to `"first"`.
        crs: The CRS stamped on the returned collection. Defaults to `4326`.

    Returns:
        A pyramids `FeatureCollection` with one row per input point, carrying the
        input properties plus the reduced band value(s).

    Raises:
        ValueError: If `reducer` is not a valid sampling reducer, or `gdf` is
            empty.
    """
    return PointSampler(
        scale_m=scale_m, reducer=resolve_reducer(reducer)
    ).sample_to_gdf(image, gdf, crs=crs)
