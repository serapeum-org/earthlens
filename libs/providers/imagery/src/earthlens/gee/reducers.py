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
    "REDUCERS",
    "resolve_reducer",
]


class Reducer(ABC):
    """Collapse an `ee.ImageCollection` into a single `ee.Image`.

    Subclasses are callable, so an instance can be passed anywhere the backend
    accepts a `collection -> image` operation.

    Attributes:
        name: The registry key this reducer is addressed by, and the value a
            user's `reducer=` string resolves from.
    """

    name: ClassVar[str]

    @abstractmethod
    def __call__(self, collection: Any) -> Any:
        """Reduce `collection` to one image.

        Args:
            collection: An `ee.ImageCollection`.

        Returns:
            The reduced `ee.Image`.
        """

    def __repr__(self) -> str:
        """Return the reducer's registry name, so plans and logs read cleanly."""
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
    """

    name: ClassVar[str] = "mosaic"


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

    def __repr__(self) -> str:
        """Include the percentile, since it is what distinguishes instances."""
        return f"Percentile(q={self.q!r})"


#: Every reducer addressable by name. `percentile` is absent deliberately: it
#: needs a parameter, so it is reachable only through the `percentile:<q>`
#: spelling that :func:`resolve_reducer` understands, or by passing an instance.
REDUCERS: dict[str, type[Reducer]] = {
    cls.name: cls for cls in (Mean, Median, Min, Max, Mode, Mosaic, Sum)
}

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
        ValueError: If the name is unknown, or a parameterised spec's argument is
            not a number in range.
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
            ValueError: unsupported reducer 'average'; expected one of ['max', 'mean', 'median', 'min', 'mode', 'mosaic', 'sum'] or 'percentile:<q>'

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
        raise ValueError(
            f"unsupported reducer {name!r}; expected one of "
            f"{sorted(REDUCERS)} or 'percentile:<q>'"
        )
    if argument:
        raise ValueError(f"the {name!r} reducer takes no argument, got {spec!r}")
    return REDUCERS[name]()
