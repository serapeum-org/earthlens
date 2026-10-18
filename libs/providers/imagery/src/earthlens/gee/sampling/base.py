"""The `Sampler` contract: a pixel scale, a resolved `Reducer`, one `sample()`.

Every concrete sampler in this subpackage (point, region/zonal, transect,
time-series, random/stratified) shares one small base: a frozen value object
holding `scale_m` and a `Reducer`, plus the abstract
`sample(image, geometry) -> ee.FeatureCollection` and a `_validate_geometry`
guard. The reducer is coerced from a registry key by a before-validator through
the Epic-1 `earthlens.gee.reducers` registry, so a bad reducer name fails closed
at construction rather than at sample time, and no fourth reducer whitelist is
introduced.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from earthlens.gee.reducers import Reducer, resolve_reducer, sampling_reducers

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from geopandas.geodataframe import GeoDataFrame

__all__ = ["Sampler"]


class Sampler(BaseModel, abc.ABC):
    """Frozen base for every sampler: a `scale_m` and a resolved `Reducer`.

    A subclass implements only `sample` (its strategy) and, where it restricts
    input geometry, calls `_validate_geometry` with its own allowed types. The
    `reducer` field accepts either a `Reducer` or a registry key string (e.g.
    `"mean"`, `"percentile:90"`), coerced through the shared
    `earthlens.gee.reducers` registry.

    Examples:
        - A string reducer is resolved to a registry `Reducer` at construction:
            ```python
            >>> from earthlens.gee.sampling.base import Sampler
            >>> class _Demo(Sampler):
            ...     def sample(self, image, geometry):
            ...         return None
            >>> _Demo(scale_m=30, reducer="mean").reducer.name
            'mean'

            ```
        - A bad reducer name fails closed at construction, not at sample time:
            ```python
            >>> from earthlens.gee.sampling.base import Sampler
            >>> class _Demo(Sampler):
            ...     def sample(self, image, geometry):
            ...         return None
            >>> _Demo(scale_m=30, reducer="bogus")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    #: The `OUTPUT_KIND` a `GEE` instance reports when driven by this sampler:
    #: `"vector"` keeps each feature's geometry; a sampler that collapses
    #: geometry (e.g. a time-series table) overrides this to `"tabular"`.
    output_kind: ClassVar[str] = "vector"

    scale_m: float
    reducer: Reducer = Field(default_factory=lambda: resolve_reducer("first"))
    #: When True (the default), `reducer` must be one a `sample`/`reduceRegions`
    #: path can actually run — i.e. a name in `sampling_reducers()` (or a
    #: `percentile`). This rejects e.g. `mosaic` (an `ImageCollection`-only
    #: reducer with no `ee.Reducer`) at construction. Set False to accept any
    #: registry reducer and defer the check to Earth Engine.
    strict_reducers: bool = True

    @field_validator("reducer", mode="before")
    @classmethod
    def _coerce_reducer(cls, value: Reducer | str) -> Reducer:
        """Resolve a registry key to a `Reducer`, passing an instance through.

        Args:
            value: A `Reducer`, a registry key (`"mean"`), or a parameterised
                spec (`"percentile:90"`).

        Returns:
            The resolved `Reducer`.

        Raises:
            ValueError: If `value` is a string the registry does not know.
        """
        return resolve_reducer(value)

    @model_validator(mode="after")
    def _reducer_is_samplable(self) -> Sampler:
        """Reject a reducer with no `ee.Reducer` when `strict_reducers` is on.

        Returns:
            The sampler unchanged once the reducer is valid for a sample path.

        Raises:
            ValueError: If `strict_reducers` and `reducer` is neither a
                `sampling_reducers()` name nor a `percentile` (e.g. `mosaic`,
                which has no `ee.Reducer`); the message names the vocabulary.
        """
        name = self.reducer.name
        if (
            self.strict_reducers
            and name not in sampling_reducers()
            and name != "percentile"
        ):
            raise ValueError(
                f"reducer {name!r} has no ee.Reducer for a sample/reduceRegions "
                f"path; expected one of {sorted(sampling_reducers())} or "
                "'percentile:<q>'. Pass strict_reducers=False to defer to Earth Engine."
            )
        return self

    @abc.abstractmethod
    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Sample `image` over `geometry`, returning an `ee.FeatureCollection`.

        Args:
            image: The `ee.Image` to sample.
            geometry: A `GeoDataFrame` (or pyramids `FeatureCollection`) whose
                geometries the subclass knows how to sample.

        Returns:
            An `ee.FeatureCollection` of the sampled features.
        """

    @staticmethod
    def _validate_geometry(gdf: GeoDataFrame, allowed: frozenset[str]) -> None:
        """Raise if `gdf` is empty or holds a geometry type outside `allowed`.

        Args:
            gdf: The input `GeoDataFrame`.
            allowed: The geometry-type names this sampler accepts.

        Raises:
            ValueError: If `gdf` is empty, or a row's geometry type is not in
                `allowed` (naming the first offending row).
        """
        if len(gdf) == 0:
            raise ValueError("sampler requires a non-empty GeoDataFrame")
        offenders = [
            (row_index, geom.geom_type)
            for row_index, geom in enumerate(gdf.geometry)
            if geom.geom_type not in allowed
        ]
        if offenders:
            row_index, geom_type = offenders[0]
            raise ValueError(
                f"row {row_index} is a {geom_type}; expected one of {sorted(allowed)}"
            )
