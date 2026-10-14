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
:class:`Percentile` has to use `reduce`, so it renames the bands back.

Two capabilities, not one. A reducer can collapse a **collection** (what
`GEE(reducer=...)` asks for) and/or stand for an **`ee.Reducer`** (what
`sample_points` asks for), and the two sets are not the same: `mosaic` is an
`ImageCollection` method with no `ee.Reducer` factory at all, while `stdDev`,
`variance`, `count` and `first` are region reducers this backend does not offer
as temporal composites. So each reducer declares both - `collection_capable`
and `ee_factory` - and the two vocabularies are *derived* from the registry by
:func:`collection_reducers` and :func:`sampling_reducers` rather than restated.
Deriving one set for both is what briefly let `sample_points(reducer="mosaic")`
past the whitelist and into `AttributeError`.

Resolve a name (or an already-built object) through :func:`resolve_reducer`:

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

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import ee
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Reducer",
    "Mean",
    "Median",
    "Min",
    "Max",
    "Mode",
    "Mosaic",
    "Sum",
    "Percentile",
    "First",
    "StdDev",
    "Variance",
    "Count",
    "REDUCERS",
    "resolve_reducer",
    "collection_reducers",
    "sampling_reducers",
]


class Reducer(ABC):
    """One reducer name, in both of the senses the backend reduces things.

    A subclass declares which of the two it supports: collapsing an
    `ee.ImageCollection` into a single `ee.Image` (calling the instance) and/or
    standing for an `ee.Reducer` that collapses pixels inside a region
    (:meth:`as_ee_reducer`). Most support both, so an instance can be passed
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


class _ConvenienceReducer(Reducer):
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


class Sum(_ConvenienceReducer):
    """Per-pixel sum across the collection."""

    name: ClassVar[str] = "sum"


class Percentile(BaseModel, Reducer):
    """Per-pixel percentile across the collection.

    The reducer the previous string-dispatch mechanism could not express: Earth
    Engine has no zero-arg `collection.percentile()`, so this goes through
    `collection.reduce(ee.Reducer.percentile([q]))`. That form suffixes every band
    (`B4` becomes `B4_p90`), so the bands are renamed back to keep the output
    interchangeable with every other reducer here.

    Attributes:
        q: The percentile to compute, 0-100 inclusive.

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
            The same text as :meth:`__repr__`.
        """
        return repr(self)


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
#: spelling that :func:`resolve_reducer` understands, or by passing an instance.
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
    :func:`collection_reducers`: `mosaic` has no `ee.Reducer` factory, and the
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
