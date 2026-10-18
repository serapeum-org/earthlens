"""`RegionSampler`: per-polygon zonal statistics, server- or client-side.

`RegionSampler` reduces an image over each polygon of a `GeoDataFrame`, picking
between two strategies fixed at construction:

* `ZonalStrategy.SERVER` runs `ee.Image.reduceRegions` in Earth Engine, with a
  combined reducer built from the requested `stats` so several statistics come
  back in one call.
* `ZonalStrategy.CLIENT` reduces locally with pyramids' own
  `Dataset.zonal_stats`, for a download that is already in memory.

Both converge on one row per polygon carrying a column per requested stat. The
server arm exposes Earth Engine's `tileScale` memory knob (the aggregation that
breaks first on a large admin layer), validated to its documented `0.1`-`16`
range.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import field_validator

from earthlens.gee.reducers import resolve_reducer
from earthlens.gee.sampling.base import Sampler
from earthlens.gee.vector.geometry import GeometryConverter

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.dataset import Dataset
    from pyramids.feature import FeatureCollection

__all__ = ["RegionSampler", "ZonalStrategy", "zonal_stats"]


class ZonalStrategy(StrEnum):
    """Where a `RegionSampler` runs its per-polygon reduction."""

    SERVER = "server"
    CLIENT = "client"


class RegionSampler(Sampler):
    """Reduce an image over each polygon, server-side or client-side.

    Args:
        scale_m: The pixel scale (metres) for the reduction.
        strategy: `ZonalStrategy.SERVER` (Earth Engine) or `CLIENT` (pyramids).
            Defaults to server.
        stats: The statistics to compute, one column per entry. Each must be a
            reducer the `earthlens.gee.reducers` registry knows. Defaults to
            `("mean",)`.
        tile_scale: Earth Engine's `tileScale` for the server arm; raising it
            (to 2 or 4) lets an out-of-memory aggregation succeed. Must be within
            `0.1`-`16`. Defaults to `1.0`.

    Examples:
        - A region sampler carries its strategy and requested stats:
            ```python
            >>> from earthlens.gee.sampling.region import RegionSampler, ZonalStrategy
            >>> sampler = RegionSampler(scale_m=30, stats=("mean", "max"))
            >>> (sampler.strategy, sampler.stats, sampler.tile_scale)
            (<ZonalStrategy.SERVER: 'server'>, ('mean', 'max'), 1.0)

            ```
        - `tile_scale` is validated against Earth Engine's documented range:
            ```python
            >>> from earthlens.gee.sampling.region import RegionSampler
            >>> RegionSampler(scale_m=30, tile_scale=99)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    _POLYGON_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"Polygon", "MultiPolygon"}
    )

    strategy: ZonalStrategy = ZonalStrategy.SERVER
    stats: tuple[str, ...] = ("mean",)
    tile_scale: float = 1.0

    @field_validator("stats")
    @classmethod
    def _require_a_stat(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse an empty `stats`, which would compute nothing.

        Args:
            value: The requested statistics.

        Returns:
            `value` unchanged once non-empty.

        Raises:
            ValueError: If `value` is empty.
        """
        if not value:
            raise ValueError("stats must name at least one reducer")
        return value

    @field_validator("tile_scale")
    @classmethod
    def _check_tile_scale(cls, value: float) -> float:
        """Keep `tile_scale` within Earth Engine's documented `0.1`-`16`.

        Args:
            value: The requested tile scale.

        Returns:
            `value` unchanged once in range.

        Raises:
            ValueError: If `value` is outside `[0.1, 16]`.
        """
        if not 0.1 <= value <= 16.0:
            raise ValueError(f"tile_scale must be within [0.1, 16], got {value}")
        return value

    def _combined_reducer(self) -> Any:
        """Fold `stats` into one `ee.Reducer` that shares its inputs.

        Returns:
            A single `ee.Reducer` computing every requested stat in one pass.
        """
        reducers = [resolve_reducer(stat).as_ee_reducer() for stat in self.stats]
        combined = reducers[0]
        for extra in reducers[1:]:
            combined = combined.combine(reducer2=extra, sharedInputs=True)
        return combined

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Reduce each polygon inside Earth Engine (the server strategy).

        Args:
            image: The `ee.Image` to reduce.
            geometry: A `GeoDataFrame` of `Polygon` / `MultiPolygon` geometries.

        Returns:
            An `ee.FeatureCollection` of the input polygons, each carrying the
            requested statistic(s).

        Raises:
            ValueError: If `geometry` is empty or holds a non-polygon geometry.
        """
        self._validate_geometry(geometry, self._POLYGON_GEOM_TYPES)
        collection = image.reduceRegions(
            collection=GeometryConverter().to_ee(geometry),
            reducer=self._combined_reducer(),
            scale=self.scale_m,
            tileScale=self.tile_scale,
        )
        return cast("ee.FeatureCollection", collection)

    def sample_to_gdf(
        self,
        image: ee.Image,
        geometry: GeoDataFrame,
        dataset: Dataset | None = None,
        *,
        crs: int | str = 4326,
    ) -> FeatureCollection:
        """Return one row per polygon, a column per requested stat.

        The server strategy pulls `sample`'s result through the
        `FeatureDownloader`; the client strategy reduces `dataset` locally with
        pyramids' `Dataset.zonal_stats` and never touches Earth Engine.

        Args:
            image: The `ee.Image` to reduce (server strategy).
            geometry: A `GeoDataFrame` of polygon geometries.
            dataset: An in-memory pyramids `Dataset`, required by the client
                strategy and ignored by the server one.
            crs: CRS stamped on the server strategy's returned collection.
                Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection` of one row per polygon.

        Raises:
            ValueError: If the client strategy is used without a `dataset`, or
                `geometry` is empty / non-polygon.
        """
        from pyramids.feature import FeatureCollection

        from earthlens.gee.vector.download import FeatureDownloader

        if self.strategy is ZonalStrategy.CLIENT:
            if dataset is None:
                raise ValueError(
                    "the client strategy needs an in-memory pyramids Dataset; "
                    "pass dataset=... (from a materialised download)"
                )
            self._validate_geometry(geometry, self._POLYGON_GEOM_TYPES)
            result = dataset.zonal_stats(
                FeatureCollection(geometry), stats=list(self.stats), band=0
            )
        else:
            result = FeatureDownloader().to_gdf(self.sample(image, geometry), crs=crs)
        return cast("FeatureCollection", result)


def zonal_stats(
    image: ee.Image,
    gdf: GeoDataFrame,
    *,
    scale_m: float,
    stats: tuple[str, ...] = ("mean",),
    strategy: str = "server",
    dataset: Dataset | None = None,
    crs: int | str = 4326,
) -> FeatureCollection:
    """Reduce an image over polygons, mirroring `sample_points`' shape.

    Args:
        image: The `ee.Image` to reduce (server strategy).
        gdf: A `GeoDataFrame` of polygon geometries.
        scale_m: The pixel scale (metres).
        stats: The statistics to compute. Defaults to `("mean",)`.
        strategy: `"server"` or `"client"`. Defaults to `"server"`.
        dataset: An in-memory pyramids `Dataset` for the client strategy.
        crs: CRS for the server strategy's result. Defaults to `4326`.

    Returns:
        A pyramids `FeatureCollection` of one row per polygon.
    """
    sampler = RegionSampler(
        scale_m=scale_m, stats=tuple(stats), strategy=ZonalStrategy(strategy)
    )
    return sampler.sample_to_gdf(image, gdf, dataset, crs=crs)
