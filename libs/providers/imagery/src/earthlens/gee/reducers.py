"""Temporal reducers as strategy objects.

A reducer collapses an `ee.ImageCollection` into one `ee.Image`. The backend used
to do that with `getattr(collection, name)()` against a frozenset of seven allowed
names, which works only for reducers Earth Engine happens to expose as a zero-arg
convenience method — so a parameterised reducer like a 90th percentile could not be
expressed at all, and three modules each kept their own copy of the allowed names.

Each reducer here is a small callable object instead. Being callable means a
`Reducer` satisfies the same `collection -> image` shape the rest of the backend
already passes around, and a parameterised one carries its parameter as validated
state rather than needing a second argument threaded through every call site.

The seven original names keep their exact semantics: they dispatch to Earth
Engine's convenience methods, which **preserve band names**, where
`collection.reduce(ee.Reducer.mean())` would rename `B4` to `B4_mean`.
`Percentile` has to use `reduce`, so it renames the bands back.

Two capabilities, not one. A reducer can collapse a **collection** (what
`GEE(reducer=...)` asks for) and/or stand for an **`ee.Reducer`** (what
`sample_points` asks for), and the two sets are not the same: `mosaic` is an
`ImageCollection` method with no `ee.Reducer` factory at all, while `stdDev`,
`variance`, `count` and `first` are region reducers this backend does not offer
as temporal composites. So each reducer declares both - `collection_capable`
and `ee_factory` - and the two vocabularies are *derived* from the registry by
`collection_reducers` and `sampling_reducers` rather than restated.
Deriving one set for both is what briefly let `sample_points(reducer="mosaic")`
past the whitelist and into `AttributeError`.

The named-algorithm composites (`LandsatComposite` /
`Sentinel2Composite`) are `TemporalReducer`s too, reachable only as
instances (`GEE(reducer=LandsatComposite())`): they wrap
`ee.Algorithms.Landsat.simpleComposite` and an
`ee.Algorithms.Sentinel2.CDI`-masked reduction, which exist only
server-side, so the EEDAI reader declines them and the request runs on
an Earth Engine engine.

Resolve a name (or an already-built object) through `resolve_reducer`:

```python
>>> resolve_reducer("median").name
'median'
>>> resolve_reducer("percentile:90").q
90.0
>>> resolve_reducer(Percentile(q=95)).name
'percentile'

```
"""

from __future__ import annotations

import functools
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

import ee
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from earthlens.gee.cloud_masks import Sentinel2Cdi, _checked_cdi_threshold

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from pyramids.dataset import Dataset, DatasetCollection

__all__ = [
    "Reducer",
    "TemporalReducer",
    "Mean",
    "Median",
    "Min",
    "Max",
    "Mode",
    "Mosaic",
    "Sum",
    "Percentile",
    "LinearTrend",
    "SensSlope",
    "LandsatComposite",
    "Sentinel2Composite",
    "First",
    "StdDev",
    "Variance",
    "Count",
    "REDUCERS",
    "resolve_reducer",
    "temporal_reducer_for",
    "collection_reducers",
    "sampling_reducers",
]


class Reducer(ABC):
    """One reducer name, in both of the senses the backend reduces things.

    A subclass declares which of the two it supports: collapsing an
    `ee.ImageCollection` into a single `ee.Image` (calling the instance) and/or
    standing for an `ee.Reducer` that collapses pixels inside a region
    (`as_ee_reducer`). Most support both, so an instance can be passed
    anywhere the backend accepts a `collection -> image` operation; a
    region-only one refuses the call instead.

    Attributes:
        name: The registry key this reducer is addressed by, and the value a
            user's `reducer=` string resolves from.
        collection_capable: Whether this reducer can collapse a collection, i.e.
            whether `GEE(reducer=...)` accepts it. `False` for the region-only
            reducers, which are registry members so that one vocabulary covers
            both paths, not so that they become temporal composites.
        ee_factory: The `ee.Reducer` factory name this reducer stands for, or
            `None` when it has no `ee.Reducer` equivalent. `mosaic` is the one
            such case: it is an `ImageCollection` method, and `ee.Reducer` has no
            `mosaic` at all.
    """

    name: ClassVar[str]
    collection_capable: ClassVar[bool] = True
    ee_factory: ClassVar[str | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Default `ee_factory` to the reducer's own name.

        Earth Engine spells the collection method and the reducer factory the
        same way for almost every reducer, so restating the name per class would
        only create somewhere for the two to drift. A class that declares
        `ee_factory` itself keeps what it declared - which is how `Mosaic` says
        it has no factory at all, and `Percentile` points at the parameterised
        one. An intermediate base with no `name` of its own is not a reducer
        yet, so it is left alone.

        Args:
            **kwargs: Forwarded to the base implementation.
        """
        super().__init_subclass__(**kwargs)
        name = cls.__dict__.get("name")
        if name is not None and "ee_factory" not in cls.__dict__:
            cls.ee_factory = name

    @abstractmethod
    def __call__(self, collection: Any) -> Any:
        """Reduce `collection` to one image.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`.
        """

    def as_ee_reducer(self) -> Any:
        """Return the `ee.Reducer` this reducer stands for.

        What a *region* reduction needs: `sample_points` reduces pixels inside a
        geometry, which takes an `ee.Reducer` rather than a collection method.
        Built from the declared factory so the two paths cannot disagree about
        which reducer a name means.

        Returns:
            The `ee.Reducer` instance, e.g. `ee.Reducer.mean()`.

        Raises:
            ValueError: If this reducer has no `ee.Reducer` equivalent, which is
                `mosaic` and nothing else. Raised rather than returning `None`,
                because the caller's next move would be to call it.
        """
        if self.ee_factory is None:
            raise ValueError(
                f"the {self.name!r} reducer is a collection method with no "
                "ee.Reducer equivalent, so it cannot reduce a region; use one "
                f"of {sorted(sampling_reducers())}"
            )
        return getattr(ee.Reducer, self.ee_factory)()

    def __repr__(self) -> str:
        """Return `ClassName()`, so plans and log lines read cleanly.

        Returns:
            The class name with empty parentheses, e.g. `Median()`. A
            parameterised reducer overrides this to show its parameter.
        """
        return f"{type(self).__name__}()"


class TemporalReducer(Reducer):
    """A reducer that also knows the time axis, both server- and client-side.

    The time-axis counterpart to the plain `Reducer`: besides collapsing an
    `ee.ImageCollection` server-side (`apply_ee`), it can collapse a pyramids
    `DatasetCollection` scene stack client-side (`reduce_stack`), which the EEDAI
    path needs when it composites locally rather than on Earth Engine. The seven
    band-name-preserving convenience reducers and the parameterised `Percentile`
    / `LinearTrend` are all `TemporalReducer`s, so `temporal_reducer_for` returns
    one for every name it understands.

    `apply_ee` keeps the fast path: for the zero-arg names it delegates to
    `reduce_collection`, which dispatches to the collection's own convenience
    method and so preserves band names. `reduce_stack` maps the reducer onto
    `DatasetCollection.reduce_time` over a single whole-stack window.
    """

    def apply_ee(self, collection: Any) -> Any:
        """Collapse `collection` to one `ee.Image`, server-side.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`, with the original band names preserved.

        Examples:
            - The zero-arg names dispatch to the band-preserving convenience
              method, here stood in for by a tiny fake collection:
                ```python
                >>> class _Collection:
                ...     def median(self):
                ...         return "median-image"
                >>> Median().apply_ee(_Collection())
                'median-image'

                ```
        """
        from earthlens.gee._helpers import reduce_collection

        return reduce_collection(collection, self.name)

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Collapse a `DatasetCollection` scene stack to one `Dataset`.

        Reduces every timestep into a single whole-window `Dataset` through
        pyramids' `DatasetCollection.reduce_time`, the client-side counterpart to
        `apply_ee`.

        Args:
            stack: The scene stack to reduce.
            times: One timestamp per timestep, in stack order.

        Returns:
            The reduced `Dataset`.
        """
        return _reduce_whole_window(stack, times, op=self.name, q=None)


class _ConvenienceReducer(TemporalReducer):
    """A reducer backed by one of Earth Engine's zero-arg collection methods.

    These are the reducers Earth Engine exposes directly on `ImageCollection`
    (`mean()`, `median()`, …). They matter because they **keep the original band
    names**, which the generic `reduce(ee.Reducer.X())` form does not.
    """

    def __call__(self, collection: Any) -> Any:
        """Dispatch to the collection method matching this reducer's `name`.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`, with band names unchanged.
        """
        return getattr(collection, self.name)()


class Mean(_ConvenienceReducer):
    """Per-pixel arithmetic mean across the collection."""

    name: ClassVar[str] = "mean"


class Median(_ConvenienceReducer):
    """Per-pixel median across the collection."""

    name: ClassVar[str] = "median"


class Min(_ConvenienceReducer):
    """Per-pixel minimum across the collection."""

    name: ClassVar[str] = "min"


class Max(_ConvenienceReducer):
    """Per-pixel maximum across the collection."""

    name: ClassVar[str] = "max"


class Mode(_ConvenienceReducer):
    """Per-pixel modal (most frequent) value across the collection."""

    name: ClassVar[str] = "mode"


class Mosaic(_ConvenienceReducer):
    """Last-wins mosaic: the latest unmasked pixel in collection order.

    Note this is Earth Engine's server-side meaning. A client-side reader that
    composites scene by scene cannot reproduce it, which is why the EEDAI path
    declines this reducer rather than serving something subtly different.

    It is also the one reducer with no `ee.Reducer` equivalent, so it cannot
    reduce a region: `ee.Reducer.mosaic` does not exist.
    """

    name: ClassVar[str] = "mosaic"
    ee_factory: ClassVar[str | None] = None

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Refuse: a last-wins mosaic has no client-side equivalent.

        Earth Engine's `mosaic` keeps the latest unmasked pixel in collection
        order, which a scene-by-scene client-side stack cannot reproduce; the
        EEDAI path composites such a request on Earth Engine instead.

        Args:
            stack: Ignored — there is no client-side reduction to run.
            times: Ignored, for the same reason.

        Raises:
            ValueError: Always.
        """
        raise ValueError(
            "the 'mosaic' reducer is last-wins in Earth Engine's collection "
            "order, which has no client-side equivalent over a scene stack; "
            "composite it on Earth Engine instead"
        )


class Sum(_ConvenienceReducer):
    """Per-pixel sum across the collection."""

    name: ClassVar[str] = "sum"


class Percentile(BaseModel, TemporalReducer):
    """Per-pixel percentile across the collection.

    The reducer the previous string-dispatch mechanism could not express: Earth
    Engine has no zero-arg `collection.percentile()`, so this goes through
    `collection.reduce(ee.Reducer.percentile([q]))`. That form suffixes every band
    (`B4` becomes `B4_p90`), so the bands are renamed back to keep the output
    interchangeable with every other reducer here.

    Attributes:
        q: The percentile to compute, `0 <= q <= 100`. Zero is accepted at
            construction because it is a valid region reducer (the minimum);
            only the temporal path refuses it (see `temporal_reducer_for`),
            where the client-side `reduce_time(op="percentile")` rejects it, so
            accepting it there would defer the failure to mid-run.

    Examples:
        - A 90th-percentile composite, which is a common cloud-robust choice:
            ```python
            >>> Percentile(q=90).q
            90.0

            ```
        - Out-of-range values are rejected at construction:
            ```python
            >>> Percentile(q=120)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for Percentile
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ClassVar[str] = "percentile"
    ee_factory: ClassVar[str | None] = "percentile"

    q: float = Field(ge=0, le=100)

    def __call__(self, collection: Any) -> Any:
        """Reduce `collection` to its per-pixel `q`-th percentile.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`, with the original band names restored.
        """
        band_names = collection.first().bandNames()
        reduced = collection.reduce(ee.Reducer.percentile([self.q]))
        return reduced.rename(band_names)

    def apply_ee(self, collection: Any) -> Any:
        """Collapse `collection` to its per-pixel percentile, server-side.

        The parameterised reducer `reduce_collection` cannot express (it has no
        bare name), so it reuses this class's own `reduce`-and-rename call.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`, with the original band names restored.
        """
        return self(collection)

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Collapse a `DatasetCollection` to its per-pixel percentile `Dataset`.

        Args:
            stack: The scene stack to reduce.
            times: One timestamp per timestep, in stack order.

        Returns:
            The reduced `Dataset`.
        """
        return _reduce_whole_window(stack, times, op="percentile", q=self.q)

    def as_ee_reducer(self) -> Any:
        """Return `ee.Reducer.percentile([q])`.

        Overridden because this is the one factory that takes an argument, which
        is also what makes the reducer reachable from the region path at all: a
        percentile could not be expressed as a bare whitelist name.

        Returns:
            The `ee.Reducer` for this instance's percentile.
        """
        return ee.Reducer.percentile([self.q])

    def __repr__(self) -> str:
        """Include the percentile, since it is what distinguishes instances."""
        return f"Percentile(q={self.q!r})"

    def __str__(self) -> str:
        """Match `__repr__`, which pydantic's own `__str__` would otherwise hide.

        A `BaseModel` prints as its fields (`q=90.0`), so `print(reducer)` in a
        log line or a notebook said nothing about *which* reducer it was.

        Returns:
            The same text as `__repr__`.
        """
        return repr(self)


#: The scene property Earth Engine stamps each image's acquisition time in,
#: in milliseconds since the Unix epoch.
_TIME_PROPERTY = "system:time_start"

#: The band the temporal-slope reducers regress each value band against: the
#: scene timestamp rescaled to days, so the fitted slope comes out in value-units
#: per day — the same convention the client-side `reduce_time` trend uses.
_TIME_BAND = "time"

#: Milliseconds per day, the divisor turning `system:time_start` into days.
_MILLIS_PER_DAY = 1000 * 60 * 60 * 24


def _stamp_time_band(image: Any) -> Any:
    """Add a day-scaled `time` band to one scene, as a slope's x-axis.

    Args:
        image: One `ee.Image` of the collection.

    Returns:
        The image with its `system:time_start` added as a `time` band, in days
        since the epoch, so a regression against it yields a per-day slope.
    """
    image = ee.Image(image)
    days = image.metadata(_TIME_PROPERTY).divide(_MILLIS_PER_DAY).rename(_TIME_BAND)
    return image.addBands(days)


def _to_double(image: Any) -> Any:
    """Cast one stamped scene to double, so the slope reducer sees one type.

    `ee.Reducer.linearFit()` / `sensSlope()` are two-input reducers, and Earth
    Engine requires the two inputs to be type-homogeneous across every scene of
    the collection. The value bands arrive in their native per-dataset types,
    and Earth Engine infers that type from each scene's value range — so two
    constant scenes of `1` and `5` come back as `Byte<1, 1>` and `Byte<5, 5>`,
    which it rejects as incompatible (`Mismatched type for band ...`), quite
    apart from the stamped double `time` band. Casting each stamped
    `[time, value]` scene to a 64-bit float collapses every band to one common
    floating type, so the collection is homogeneous.

    Args:
        image: One stamped `ee.Image` carrying the `time` band and value bands.

    Returns:
        The image with every band cast to a 64-bit float.
    """
    return ee.Image(image).toDouble()


def _fit_band(name: Any, *, stamped: Any, reducer: Any, slope_band: str) -> Any:
    """Fit one value band against time and return its renamed slope band.

    Args:
        name: The value band to fit, an `ee.String` server-side.
        stamped: The collection carrying the `time` band and the value bands.
        reducer: The two-input `ee.Reducer` fitting `[time, value]`
            (`linearFit` or `sensSlope`).
        slope_band: The reducer's slope output band (`scale` / `slope`).

    Returns:
        The per-pixel slope `ee.Image` for this band, renamed back to it.
    """
    name = ee.String(name)
    pair = stamped.select(ee.List([_TIME_BAND]).add(name))
    slope = pair.reduce(reducer).select([slope_band])
    return slope.rename([name])


def _temporal_slope_ee(collection: Any, *, reducer: Any, slope_band: str) -> Any:
    """Fit a per-pixel temporal slope for every value band, server-side.

    Stamps a `time` band on each scene, casts every stamped scene to double so
    the two-input reducer sees one homogeneous type across the collection (see
    `_to_double`), fits `[time, value]` per value band with `reducer`, selects
    the slope output band and renames it back to the value band — a real trend
    over time, in value-units per day. Without the time band,
    `ee.Reducer.linearFit()` / `sensSlope()` would see the value bands as their
    two inputs and regress them against each other rather than against time (and
    raise on single-band data).

    Args:
        collection: The filtered `ee.ImageCollection` to reduce.
        reducer: The two-input `ee.Reducer` (`linearFit` or `sensSlope`).
        slope_band: The reducer's slope output band (`scale` / `slope`).

    Returns:
        The reduced `ee.Image` of per-pixel slopes, carrying the input bands.
    """
    band_names = collection.first().bandNames()
    stamped = collection.map(_stamp_time_band).map(_to_double)
    fit = functools.partial(
        _fit_band, stamped=stamped, reducer=reducer, slope_band=slope_band
    )
    slopes = band_names.map(fit)
    return ee.ImageCollection.fromImages(slopes).toBands().rename(band_names)


class LinearTrend(TemporalReducer):
    """Per-pixel linear trend across the collection, over time.

    On the Earth Engine path it stamps a day-scaled `time` band on each scene
    and fits `[time, value]` per value band with `ee.Reducer.linearFit()`,
    selecting the `scale` (slope) output back into the value band; on the
    client-side path it is `reduce_time(op="trend")`. Both yield the per-pixel
    least-squares slope in value-units per day, under the input band names. Like
    `Percentile` it is deliberately **not** in `REDUCERS` — it is reachable only
    through `temporal_reducer_for` (as `"trend"`) or by constructing it
    directly, so the seven-name `REDUCERS` registry / `collection_reducers()`
    set is unchanged (`GEE(reducer=...)` itself validates through
    `temporal_reducer_for`, which also accepts `trend` / `sens_slope` /
    `percentile`).
    """

    name: ClassVar[str] = "trend"
    ee_factory: ClassVar[str | None] = None

    def __call__(self, collection: Any) -> Any:
        """Reduce `collection` to its per-pixel linear trend over time.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The `ee.Image` of per-pixel least-squares slopes (value-units per
            day), carrying the input band names.
        """
        return _temporal_slope_ee(
            collection, reducer=ee.Reducer.linearFit(), slope_band="scale"
        )

    def apply_ee(self, collection: Any) -> Any:
        """Collapse `collection` to its linear trend `ee.Image`, server-side.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image` of per-pixel slopes (value-units per day).
        """
        return self(collection)

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Collapse a `DatasetCollection` to its per-pixel trend `Dataset`.

        Args:
            stack: The scene stack to reduce.
            times: One timestamp per timestep; the trend's x-axis.

        Returns:
            The `Dataset` of per-pixel slopes (value-units per day).
        """
        return _reduce_whole_window(stack, times, op="trend", q=None)


class SensSlope(TemporalReducer):
    """Per-pixel Theil-Sen slope (and offset) across the collection.

    The robust counterpart to `LinearTrend`: where `LinearTrend` fits the
    ordinary least-squares line, `SensSlope` takes the median of the pairwise
    slopes (Theil-Sen), so a few outlier scenes cannot drag the trend. On the
    Earth Engine path it stamps a day-scaled `time` band on each scene and fits
    `[time, value]` per value band with `ee.Reducer.sensSlope()`, selecting the
    `slope` output back into the value band; on the client-side path it is
    `reduce_time(op="sens_slope")`. Both yield the per-pixel Theil-Sen slope in
    value-units per day, under the input band names. Like `LinearTrend` it is
    deliberately **not** in `REDUCERS` — it is reachable only through
    `temporal_reducer_for` (as `"sens_slope"`) or by constructing it directly,
    so the seven-name `REDUCERS` registry / `collection_reducers()` set is
    unchanged (`GEE(reducer=...)` itself validates through
    `temporal_reducer_for`, which also accepts `trend` / `sens_slope` /
    `percentile`).
    """

    name: ClassVar[str] = "sens_slope"
    ee_factory: ClassVar[str | None] = None

    def __call__(self, collection: Any) -> Any:
        """Reduce `collection` to its per-pixel Theil-Sen slope over time.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The `ee.Image` of per-pixel Theil-Sen slopes (value-units per day),
            carrying the input band names.
        """
        return _temporal_slope_ee(
            collection, reducer=ee.Reducer.sensSlope(), slope_band="slope"
        )

    def apply_ee(self, collection: Any) -> Any:
        """Collapse `collection` to its Theil-Sen slope `ee.Image`, server-side.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image` of per-pixel slopes (value-units per day).
        """
        return self(collection)

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Collapse a `DatasetCollection` to its per-pixel Theil-Sen `Dataset`.

        Args:
            stack: The scene stack to reduce.
            times: One timestamp per timestep; the trend's x-axis.

        Returns:
            The `Dataset` of per-pixel Theil-Sen slopes (value-units per day).
        """
        return _reduce_whole_window(stack, times, op="sens_slope", q=None)


class _NamedAlgorithmComposite(BaseModel, TemporalReducer):
    """A composite backed by one of Earth Engine's named algorithms.

    The shared shape of `LandsatComposite` / `Sentinel2Composite`:
    frozen-pydantic config, a `compose` alias naming the
    collection-to-image intent, and refusals for the two seams a named
    algorithm cannot serve — the client-side stack reduction (the
    algorithm exists only server-side, so the EEDAI reader declines it
    rather than approximating) and the region reduction (there is no
    `ee.Reducer` equivalent). Deliberately **not** in `REDUCERS`: like
    `Percentile`, a composite is reachable only as an instance, so the
    seven-name registry and `collection_reducers()` are unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def compose(self, collection: Any) -> Any:
        """Build the composite image; the explicit alias of calling it.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The composited `ee.Image`.
        """
        return self(collection)

    def apply_ee(self, collection: Any) -> Any:
        """Collapse `collection` through the named algorithm, server-side.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The composited `ee.Image`.
        """
        return self(collection)

    def reduce_stack(self, stack: DatasetCollection, times: Sequence) -> Dataset:
        """Refuse: a named algorithm exists only server-side.

        Args:
            stack: Ignored — there is no client-side reduction to run.
            times: Ignored, for the same reason.

        Raises:
            ValueError: Always.
        """
        raise ValueError(
            f"the {self.name!r} composite runs an Earth Engine named algorithm, "
            "which has no client-side equivalent over a scene stack; composite "
            "it on Earth Engine instead (engine='ee')"
        )

    def as_ee_reducer(self) -> Any:
        """Refuse: a named algorithm has no `ee.Reducer` equivalent.

        Raises:
            ValueError: Always, naming the reducers that do reduce a
                region.
        """
        raise ValueError(
            f"the {self.name!r} composite wraps an Earth Engine named algorithm "
            "with no ee.Reducer equivalent, so it cannot reduce a region; use "
            f"one of {sorted(sampling_reducers())}"
        )

    def __str__(self) -> str:
        """Match `__repr__`, which pydantic's own `__str__` would hide.

        A `BaseModel` prints as its fields alone, so `print(reducer)` in
        a log line would not say *which* composite it was.

        Returns:
            The same text as `__repr__`.
        """
        return repr(self)


class LandsatComposite(_NamedAlgorithmComposite):
    """A cloud-reduced Landsat TOA composite via `simpleComposite`.

    Wraps `ee.Algorithms.Landsat.simpleComposite`, which assembles a
    cloud-screened, TOA-calibrated mosaic from **raw Level-1 scenes** in
    one server call: it computes a simple cloud score per scene and
    takes the `percentile` of the least-cloudy pixels. Plug it into the
    facade as an instance — `GEE(reducer=LandsatComposite())` — and the
    composite engine applies it per time bucket.

    The input must be a **raw L1 collection** — e.g. the curated
    `LANDSAT/LC08/C02/T1` catalog row — not the `T1_L2`
    surface-reflectance product: the algorithm performs its own TOA
    calibration and rejects already-processed inputs. A named algorithm
    exists only server-side, so the EEDAI reader declines this reducer
    and the request runs on an Earth Engine engine.

    Attributes:
        percentile: The percentile of the cloud-screened stack to take
            per pixel, `0`-`100`. Defaults to `50` (the algorithm's
            documented default).
        cloud_score_range: The cloud-score margin (`cloudScoreRange`)
            above the per-pixel minimum within which scenes still
            contribute. Defaults to `10`.
        max_depth: The maximum number of scenes considered per pixel
            (`maxDepth`). Defaults to `40`.
        as_float: Return floating-point TOA reflectance (`asFloat`)
            instead of the 8-bit scaled default. Defaults to `False`.

    Examples:
        - The defaults are the algorithm's documented ones:
            ```python
            >>> composite = LandsatComposite()
            >>> composite.percentile, composite.max_depth, composite.as_float
            (50, 40, False)

            ```
        - An out-of-range percentile is refused at construction:
            ```python
            >>> LandsatComposite(percentile=150)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for LandsatComposite
            ...

            ```
        - Hand an instance to the GEE backend as the temporal reducer:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="LANDSAT/LC08/C02/T1",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2023-06-01", end="2023-09-01",
            ...     reducer=LandsatComposite(as_float=True),
            ... )

            ```
    """

    name: ClassVar[str] = "landsat_composite"
    ee_factory: ClassVar[str | None] = None

    percentile: int = Field(default=50, ge=0, le=100)
    cloud_score_range: float = Field(default=10.0, gt=0)
    max_depth: int = Field(default=40, gt=0)
    as_float: bool = False

    def __call__(self, collection: Any) -> Any:
        """Compose `collection` with `ee.Algorithms.Landsat.simpleComposite`.

        Args:
            collection: An `ee.ImageCollection` of **raw L1** Landsat
                scenes (e.g. `LANDSAT/LC08/C02/T1`).

        Returns:
            The cloud-reduced TOA composite `ee.Image`.
        """
        return ee.Algorithms.Landsat.simpleComposite(
            collection=collection,
            percentile=self.percentile,
            cloudScoreRange=self.cloud_score_range,
            maxDepth=self.max_depth,
            asFloat=self.as_float,
        )


class Sentinel2Composite(_NamedAlgorithmComposite):
    """A CDI-masked Sentinel-2 TOA composite.

    Maps the `earthlens.gee.cloud_masks.Sentinel2Cdi` mask —
    `ee.Algorithms.Sentinel2.CDI`, the parallax-based cloud
    discriminator — over every scene, then collapses the masked stack
    with the inner `reducer` (a per-pixel median by default). Plug it
    into the facade as an instance —
    `GEE(reducer=Sentinel2Composite())` — and the composite engine
    applies it per time bucket.

    CDI is only *valid* on the **TOA** product
    (`COPERNICUS/S2_HARMONIZED`, or the legacy `COPERNICUS/S2`), not L2A:
    it is a parallax computation over the TOA band geometry. On surface
    reflectance the graph does **not** fail — verified live, CDI computes
    a value there — so the hazard is a silently different mask rather
    than an error, and the `GEE` backend refuses
    `reducer=Sentinel2Composite()` against
    `COPERNICUS/S2_SR_HARMONIZED` / `COPERNICUS/S2_SR` at construction.
    A named algorithm exists only server-side, so the EEDAI reader
    declines this reducer and the request runs on an Earth Engine engine.

    Attributes:
        threshold: The CDI value below which a pixel is masked before
            the reduction; a `[-1, 1]` index, validated at
            construction. Defaults to `-0.5`.
        reducer: The temporal reducer applied to the CDI-masked stack.
            Defaults to the median; accepts any `temporal_reducer_for`
            spelling or instance.

    Examples:
        - The defaults: the recommended CDI operating point, reduced by
          the median:
            ```python
            >>> composite = Sentinel2Composite()
            >>> composite.threshold, composite.reducer.name
            (-0.5, 'median')

            ```
        - The inner reducer resolves through the one registry:
            ```python
            >>> Sentinel2Composite(reducer="p90").reducer.q
            90.0

            ```
        - Hand an instance to the GEE backend as the temporal reducer:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_HARMONIZED",
            ...     variables=["B4", "B3", "B2"],
            ...     start="2024-05-01", end="2024-06-15",
            ...     reducer=Sentinel2Composite(threshold=-0.4),
            ... )

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: ClassVar[str] = "sentinel2_composite"
    ee_factory: ClassVar[str | None] = None

    threshold: float = -0.5
    reducer: TemporalReducer = Field(default_factory=Median)

    @field_validator("threshold")
    @classmethod
    def _in_cdi_range(cls, value: float) -> float:
        """Refuse a threshold outside the inclusive `[-1, 1]` CDI range.

        Args:
            value: The parsed `threshold` input.

        Returns:
            The value unchanged when it is in range.

        Raises:
            ValueError: If `value` is outside `[-1, 1]`.
        """
        return _checked_cdi_threshold(value)

    @field_validator("reducer", mode="before")
    @classmethod
    def _coerce_reducer(cls, value: TemporalReducer | str) -> TemporalReducer:
        """Resolve the inner reducer through the one registry.

        Args:
            value: A `TemporalReducer`, a registry name (`"median"`), or
                a percentile spec / shorthand (`"percentile:90"`,
                `"p90"`).

        Returns:
            The resolved `TemporalReducer`.

        Raises:
            ValueError: If `value` names a region-only or unknown
                reducer.
        """
        return temporal_reducer_for(value)

    def __call__(self, collection: Any) -> Any:
        """Mask every scene by its CDI, then reduce the masked stack.

        Args:
            collection: An `ee.ImageCollection` of **TOA** Sentinel-2
                scenes (`COPERNICUS/S2_HARMONIZED`).

        Returns:
            The composited `ee.Image`.
        """
        masked = collection.map(Sentinel2Cdi(threshold=self.threshold))
        return self.reducer.apply_ee(masked)


class _RegionReducer(Reducer):
    """A reducer that reduces pixels in a region, not images in a collection.

    Registry members so that `reducer=` has one vocabulary across the backend,
    but not temporal composites: Earth Engine exposes no band-name-preserving
    `ImageCollection.stdDev()`, and offering one through `reduce()` would be a
    new capability rather than the refactor this epic is.
    """

    collection_capable: ClassVar[bool] = False

    def __call__(self, collection: Any) -> Any:
        """Refuse: this reducer has no collection form.

        Args:
            collection: Ignored — there is no reduction to apply it to.

        Raises:
            ValueError: Always, naming the reducers that do collapse a
                collection. `GEE(...)` refuses one of these at construction, so
                this is the backstop for a caller reaching the object directly.
        """
        raise ValueError(
            f"the {self.name!r} reducer reduces pixels in a region, not images "
            f"in a collection; use one of {sorted(collection_reducers())}"
        )


class First(_RegionReducer):
    """The first unmasked value in a region, for a region reduction only.

    Reachable through `sample_points(reducer="first")`; `GEE(reducer="first")`
    is refused, because there is no collection form to composite with.
    """

    name: ClassVar[str] = "first"


class StdDev(_RegionReducer):
    """Sample standard deviation over a region, for a region reduction only.

    Reachable through `sample_points(reducer="stdDev")`; `GEE(reducer="stdDev")`
    is refused, because there is no collection form to composite with.
    """

    name: ClassVar[str] = "stdDev"


class Variance(_RegionReducer):
    """Sample variance over a region, for a region reduction only.

    Reachable through `sample_points(reducer="variance")`;
    `GEE(reducer="variance")` is refused, because there is no collection form to
    composite with.
    """

    name: ClassVar[str] = "variance"


class Count(_RegionReducer):
    """Count of unmasked pixels in a region, for a region reduction only.

    Reachable through `sample_points(reducer="count")`; `GEE(reducer="count")`
    is refused, because there is no collection form to composite with.
    """

    name: ClassVar[str] = "count"


#: Every reducer addressable by name. `percentile` is absent deliberately: it
#: needs a parameter, so it is reachable only through the `percentile:<q>`
#: spelling that `resolve_reducer` understands, or by passing an instance.
REDUCERS: dict[str, type[Reducer]] = {
    cls.name: cls
    for cls in (
        Mean,
        Median,
        Min,
        Max,
        Mode,
        Mosaic,
        Sum,
        First,
        StdDev,
        Variance,
        Count,
    )
}


def collection_reducers() -> frozenset[str]:
    """Return the names that can collapse an `ee.ImageCollection`.

    The vocabulary `GEE(reducer=...)` accepts, and the one the EEDAI reader's
    eligibility check reads. Derived from each reducer's own
    `collection_capable`, so adding a reducer cannot leave this set behind.

    Returns:
        The collection-capable registry names.

    Examples:
        - The seven band-name-preserving convenience reducers:
            ```python
            >>> sorted(collection_reducers())
            ['max', 'mean', 'median', 'min', 'mode', 'mosaic', 'sum']

            ```
    """
    return frozenset(name for name, cls in REDUCERS.items() if cls.collection_capable)


def sampling_reducers() -> frozenset[str]:
    """Return the names that stand for an `ee.Reducer`.

    The vocabulary a *region* reduction accepts. It is not the same set as
    `collection_reducers`: `mosaic` has no `ee.Reducer` factory, and the
    region-only reducers have no collection form.

    Returns:
        The registry names carrying an `ee.Reducer` factory.

    Examples:
        - `mosaic` is absent, which is the whole reason the two sets are
          derived separately:
            ```python
            >>> sorted(sampling_reducers())
            ['count', 'first', 'max', 'mean', 'median', 'min', 'mode', 'stdDev', 'sum', 'variance']

            ```
    """
    return frozenset(
        name for name, cls in REDUCERS.items() if cls.ee_factory is not None
    )


#: Separator between a parameterised reducer's name and its argument.
_SPEC_SEPARATOR = ":"


def resolve_reducer(spec: str | Reducer) -> Reducer:
    """Resolve a reducer name, a parameterised spec, or an instance.

    Accepts an already-built `Reducer` unchanged, so a caller who wants
    `Percentile(q=95)` can pass it directly rather than round-tripping through a
    string.

    Args:
        spec: A registry name (`"median"`), a parameterised spec
            (`"percentile:90"`), or a `Reducer` instance.

    Returns:
        The resolved `Reducer`.

    Raises:
        ValueError: If the name is unknown; if `"percentile"` is given without
            its argument, or with one that is not a number between 0 and 100; or
            if a reducer that takes no argument is given one
            (`"median:3"`). The message names the alternatives.
        TypeError: If `spec` is neither a string nor a `Reducer`.

    Examples:
        - A bare name:
            ```python
            >>> resolve_reducer("mean").name
            'mean'

            ```
        - A parameterised percentile:
            ```python
            >>> resolve_reducer("percentile:25").q
            25.0

            ```
        - An unknown name names the alternatives:
            ```python
            >>> resolve_reducer("average")
            Traceback (most recent call last):
                ...
            ValueError: unsupported reducer 'average'; expected one of ['max', 'mean', 'median', 'min', 'mode', 'mosaic', 'sum'] or 'percentile:<q>' for a collection, or ['count', 'first', 'stdDev', 'variance'] for a region sample

            ```
    """
    if isinstance(spec, Reducer):
        return spec
    if not isinstance(spec, str):
        raise TypeError(
            f"reducer must be a name or a Reducer instance, got {type(spec).__name__}"
        )
    name, _, argument = spec.partition(_SPEC_SEPARATOR)
    if name == Percentile.name:
        if not argument:
            raise ValueError(
                "the percentile reducer needs its percentile, as "
                "'percentile:<q>' (e.g. 'percentile:90')"
            )
        try:
            q = float(argument)
        except ValueError:
            raise ValueError(
                f"the percentile reducer's argument must be a number, got {argument!r}"
            ) from None
        return Percentile(q=q)
    if name not in REDUCERS:
        # The two vocabularies are named separately, because the registry holds
        # both and a caller who mistyped a temporal reducer should not be
        # offered `stdDev` as though it would composite a time bucket.
        raise ValueError(
            f"unsupported reducer {name!r}; expected one of "
            f"{sorted(collection_reducers())} or 'percentile:<q>' for a "
            f"collection, or {sorted(sampling_reducers() - collection_reducers())} "
            "for a region sample"
        )

    if argument:
        raise ValueError(f"the {name!r} reducer takes no argument, got {spec!r}")
    return REDUCERS[name]()


def _reduce_whole_window(
    stack: DatasetCollection, times: Sequence, *, op: str, q: float | None
) -> Dataset:
    """Reduce an entire scene stack into one `Dataset` with `op`.

    pyramids' `DatasetCollection.reduce_time` buckets by a pandas frequency, so
    there is no literal "whole window" alias; a day multiple spanning the full
    `times` range groups every timestep into one window, which is what a single
    composite bucket reduces to.

    Args:
        stack: The scene stack to reduce.
        times: One timestamp per timestep, in stack order.
        op: The pyramids `reduce_time` operation (`"median"`, `"trend"`, ...).
        q: The percentile level for `op="percentile"`, else `None`.

    Returns:
        The single reduced `Dataset`.
    """
    index = pd.DatetimeIndex(pd.to_datetime(list(times)))
    span_days = int((index.max() - index.min()).days) + 1
    reduced = stack.reduce_time(list(times), freq=f"{span_days}D", op=op, q=q)
    return reduced[0][1]


#: A percentile shorthand like `p90`: the registry-friendly spelling of
#: `percentile:90` that `temporal_reducer_for` also accepts. Applied with
#: `fullmatch`: under `match`, the `$` also matches just before a trailing
#: newline, so `"p90\n"` would resolve rather than being reported as unknown.
_PERCENTILE_SHORTHAND = re.compile(r"^p(\d{1,3})$")

#: The message both region-only refusals share, since the suggestion is the same
#: whether the offending reducer arrived as an instance or a name.
_NO_TIME_AXIS = (
    "reduces pixels in a region, not images over time; use a collection reducer "
    "or 'percentile:<q>' / 'trend' / 'sens_slope'"
)


def _reject_zero_percentile(reducer: Reducer, spec: object) -> None:
    """Refuse a `q=0` percentile on the temporal path.

    `Percentile(q=0)` is a valid region reducer (the minimum), so the model
    accepts it; but the client-side `reduce_time(op="percentile")` rejects
    `q=0`, so the temporal resolver refuses it up front rather than letting the
    failure surface mid-composite.

    Args:
        reducer: The resolved reducer to check.
        spec: The original spelling, named in the error message.

    Raises:
        ValueError: If `reducer` is a `Percentile` with `q` not greater than 0.
    """
    if isinstance(reducer, Percentile) and reducer.q <= 0:
        raise ValueError(
            f"the percentile reducer needs 0 < q <= 100 on the temporal path, "
            f"got {spec!r}"
        )


def _temporal_reducer_from_str(spec: str) -> TemporalReducer:
    """Resolve a string spec to a `TemporalReducer`.

    The string arm of `temporal_reducer_for`: a `p<q>` shorthand becomes a
    `Percentile`, `"trend"` a `LinearTrend`, and anything else is resolved
    through `resolve_reducer` and accepted only if it carries a time-axis form.

    Args:
        spec: The reducer spelling (see `temporal_reducer_for`).

    Returns:
        The resolved `TemporalReducer`.

    Raises:
        ValueError: If the shorthand is out of range, or the name resolves to a
            region-only reducer or an unknown one.
    """
    match = _PERCENTILE_SHORTHAND.fullmatch(spec)
    if match is not None:
        q = float(match.group(1))
        if not 0 < q <= 100:
            raise ValueError(
                f"the percentile shorthand needs 0 < q <= 100, got {spec!r}"
            )
        resolved: TemporalReducer = Percentile(q=q)
    elif spec == LinearTrend.name:
        resolved = LinearTrend()
    elif spec == SensSlope.name:
        resolved = SensSlope()
    else:
        reducer = resolve_reducer(spec)
        _reject_zero_percentile(reducer, spec)
        if not isinstance(reducer, TemporalReducer):
            raise ValueError(f"the {reducer.name!r} reducer {_NO_TIME_AXIS}")
        resolved = reducer
    return resolved


def temporal_reducer_for(spec: str | Reducer) -> TemporalReducer:
    """Resolve a name, a percentile shorthand, or an instance to a `TemporalReducer`.

    The time-axis counterpart to `resolve_reducer`: it returns a
    `TemporalReducer` for every reducer that can collapse a time bucket — the
    seven band-name-preserving names, a `percentile:<q>` spec or its `p<q>`
    shorthand, `trend`, and `sens_slope`. A region-only reducer has no time-axis
    form and is refused.

    Args:
        spec: A collection reducer name (`"median"`), a percentile spec
            (`"percentile:90"`) or its shorthand (`"p90"`), `"trend"`,
            `"sens_slope"`, or an already-built `TemporalReducer`.

    Returns:
        The resolved `TemporalReducer`.

    Raises:
        ValueError: If the name is a region-only reducer, an unknown name, or a
            percentile shorthand outside `0 < q <= 100`.
        TypeError: If `spec` is neither a string nor a `Reducer`.

    Examples:
        - A bare name and the percentile shorthand:
            ```python
            >>> temporal_reducer_for("median").name
            'median'
            >>> temporal_reducer_for("p90").q
            90.0

            ```
        - `trend` resolves to the linear-fit reducer, `sens_slope` to the
          Theil-Sen one:
            ```python
            >>> temporal_reducer_for("trend").name
            'trend'
            >>> temporal_reducer_for("sens_slope").name
            'sens_slope'

            ```
        - A region-only reducer has no time-axis form:
            ```python
            >>> temporal_reducer_for("stdDev")
            Traceback (most recent call last):
                ...
            ValueError: the 'stdDev' reducer reduces pixels in a region, not images over time; use a collection reducer or 'percentile:<q>' / 'trend' / 'sens_slope'

            ```
    """
    if isinstance(spec, TemporalReducer):
        _reject_zero_percentile(spec, spec)
        result: TemporalReducer = spec
    elif isinstance(spec, Reducer):
        raise ValueError(f"the {spec.name!r} reducer {_NO_TIME_AXIS}")
    elif isinstance(spec, str):
        result = _temporal_reducer_from_str(spec)
    else:
        raise TypeError(
            f"reducer must be a name or a Reducer instance, got {type(spec).__name__}"
        )
    return result
