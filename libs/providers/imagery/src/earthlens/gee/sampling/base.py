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
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from earthlens.gee.reducers import Reducer, resolve_reducer

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

    scale_m: float
    reducer: Reducer = Field(default_factory=lambda: resolve_reducer("first"))

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
