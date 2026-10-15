"""Sample raster values at point locations via Google Earth Engine.

The single public entry point, `sample_points`, takes an
`ee.Image` and a `GeoDataFrame` of point geometries and returns an
`ee.FeatureCollection` where each input point carries the reduced
raster value(s) from its surrounding pixels as feature properties.

Implementation note — the input points are first inserted into an
`rtree.index.Index` and grouped into that index's leaves; one
`image.reduceRegions` call is issued per leaf, with each leaf's
bounding box used to clip the image first. This keeps the number of
round-trips bounded by the leaf count rather than the point count
(one call per point would be wasteful) while still keeping each call's
clip box tight (one call over the full point set would force EE to
work over the entire image).

The reducer is selected by name (e.g. `"first"`, `"mean"`,
`"median"`) and resolved through a fixed whitelist on `ee.Reducer` —
no `eval`, so a bad name fails closed with a readable error rather
than executing arbitrary input through the EE attribute lookup.
"""

from __future__ import annotations

from typing import cast

import ee
import geopandas as gpd
from geopandas.geodataframe import GeoDataFrame
from rtree import index

from earthlens.gee.features import create_feature
from earthlens.gee.io import feature_collection_to_gdf as _fc_to_gdf


def _reducer_whitelist() -> frozenset[str]:
    """Return the reducer names `ee.Image.reduceRegions` accepts here.

    Every name comes from the one registry rather than being restated -
    including the region-only ones (`first`, `stdDev`, `variance`, `count`),
    which are registry members precisely so this module does not keep a second
    list.

    Derived from the registry's `ee_factory` declarations, which is narrower than
    the registry itself: `mosaic` is an `ImageCollection` method and
    `ee.Reducer.mosaic` does not exist, so taking every registry name here sent
    `reducer="mosaic"` into an `AttributeError` instead of the refusal
    `_resolve_reducer` raises.

    Returns:
        The registry names that stand for an `ee.Reducer`.
    """
    from earthlens.gee.reducers import sampling_reducers

    return sampling_reducers()


def __getattr__(name: str) -> frozenset[str]:
    """Keep `_REDUCER_WHITELIST` readable as a module attribute.

    Args:
        name: The attribute being looked up.

    Returns:
        The derived whitelist for `_REDUCER_WHITELIST`.

    Raises:
        AttributeError: For any other name.
    """
    if name == "_REDUCER_WHITELIST":
        return _reducer_whitelist()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_POINT_GEOM_TYPES: frozenset[str] = frozenset({"Point", "MultiPoint"})


def _resolve_reducer(name: str):
    """Resolve a reducer name to an `ee.Reducer` instance via the whitelist.

    Args:
        name: A reducer name (e.g. `"mean"`), or the parameterised
            `"percentile:<q>"` spec. A bare name must be one of
            `_REDUCER_WHITELIST`.

    Returns:
        The `ee.Reducer` the registry builds for `name`, e.g.
        `ee.Reducer.mean()`, or `ee.Reducer.percentile([q])` for a
        `percentile:<q>` spec.

    Raises:
        ValueError: If `name` is neither in `_REDUCER_WHITELIST` nor a
            `percentile` spec, or if it is `"percentile"` without a numeric
            percentile between 0 and 100.
    """
    from earthlens.gee.reducers import resolve_reducer

    allowed = _reducer_whitelist()
    base, _, _argument = name.partition(":")
    # `percentile:<q>` is admitted explicitly: it is the one reducer that cannot
    # be a bare whitelist name, and the registry is the only thing that knows
    # how to build it. Everything else must be a declared sampling reducer, so
    # the refusal still names the same vocabulary it always did.
    if base not in allowed and base != "percentile":
        raise ValueError(
            f"unsupported reducer {name!r}; expected one of {sorted(allowed)}"
        )
    return resolve_reducer(name).as_ee_reducer()


def sample_points(
    image: ee.Image,
    gdf: GeoDataFrame,
    *,
    scale_m: float,
    reducer: str = "first",
) -> ee.FeatureCollection:
    """Sample an Earth Engine image at every point in a `GeoDataFrame`.

    Each input point is reduced from its `scale_m`-metre neighbourhood
    with the named reducer; the resulting `ee.FeatureCollection` keeps
    the GDF's non-geometry columns as feature properties and adds the
    reduced band value(s).

    Args:
        image: The `ee.Image` to sample.
        gdf: A `GeoDataFrame` whose geometries are points; non-geometry
            columns are carried through as feature properties.
        scale_m: The pixel scale (metres) passed to
            `image.reduceRegions(scale=...)`.
        reducer: The name of an `ee.Reducer` factory; must be one of the
            supported reducers (see `_REDUCER_WHITELIST`), or the
            parameterised `"percentile:<q>"` spec, which is the one reducer
            that cannot be named on its own. Defaults to `"first"`.

    Returns:
        An `ee.FeatureCollection` of the input points, each carrying
        the reduced band value(s).

    Raises:
        ValueError: If `reducer` is neither in `_REDUCER_WHITELIST` nor a
            valid `"percentile:<q>"` spec,
            if `gdf` is empty, or if any geometry is not a `Point` /
            `MultiPoint` (sampling at a polygon would silently
            change the semantic from "sample one pixel per point" to
            "reduce each polygon's interior" — caller probably meant
            to call `image.reduceRegions` directly).
    """
    if len(gdf) == 0:
        raise ValueError("sample_points requires a non-empty GeoDataFrame")
    non_point = [
        (i, g.geom_type)
        for i, g in enumerate(gdf.geometry)
        if g.geom_type not in _POINT_GEOM_TYPES
    ]
    if non_point:
        i, gt = non_point[0]
        raise ValueError(
            f"sample_points expects Point / MultiPoint geometries; row {i} "
            f"is a {gt}. Use `image.reduceRegions` directly for polygons."
        )
    ee_reducer = _resolve_reducer(reducer)

    rtree_idx = index.Index()
    for i, geom in enumerate(gdf.geometry):
        rtree_idx.insert(i, geom.bounds)
    leaves = sorted(rtree_idx.leaves())

    collection = ee.FeatureCollection([])
    for _, indices, bbox in leaves:
        ee_bbox = ee.Geometry.BBox(bbox[0], bbox[1], bbox[2], bbox[3])
        ee_feature_col = create_feature(gdf.iloc[indices])
        clipped = image.clip(ee_bbox)
        collection = collection.merge(
            ee.FeatureCollection(
                clipped.reduceRegions(
                    collection=ee_feature_col,
                    reducer=ee_reducer,
                    scale=scale_m,
                )
            )
        )
    return cast("ee.FeatureCollection", collection)


def sample_points_to_gdf(
    image: ee.Image,
    gdf: GeoDataFrame,
    *,
    scale_m: float,
    reducer: str = "first",
    crs: int | str = 4326,
) -> gpd.GeoDataFrame:
    """Sample an Earth Engine image at points and return a `GeoDataFrame`.

    Thin composition of `sample_points` and
    `earthlens.gee.io.feature_collection_to_gdf` — call
    `sample_points` for the leaf-batched `reduceRegions` work, then
    pull the result with a single `getInfo()` and reshape into a
    `GeoDataFrame`. Suitable for small to medium point sets (the
    `getInfo()` upper bound is ~5000 features / ~10 MB); for larger
    jobs, call `sample_points` directly and pipe its output through
    `earthlens.gee.io.feature_collections_to_dataframe` instead.

    Args:
        image: The `ee.Image` to sample.
        gdf: A `GeoDataFrame` of point geometries.
        scale_m: The pixel scale (metres) passed to
            `image.reduceRegions(scale=...)`.
        reducer: The reducer name; one of `_REDUCER_WHITELIST`, or a
            `"percentile:<q>"` spec. Defaults to `"first"`.
        crs: The CRS to stamp onto the output `GeoDataFrame`. Defaults
            to `4326`.

    Returns:
        A `GeoDataFrame` with one row per input point, carrying the
        input properties plus the reduced band value(s).

    Raises:
        ValueError: If `reducer` is neither in `_REDUCER_WHITELIST` nor a
            valid `"percentile:<q>"` spec, or if `gdf` is empty.
    """
    fc = sample_points(image, gdf, scale_m=scale_m, reducer=reducer)
    return _fc_to_gdf(fc, crs=crs)
