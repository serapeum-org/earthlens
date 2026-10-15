"""The operation contract: a frozen, callable `Dataset` to `Dataset` transform.

One abstract method, `apply`, carries the whole surface of this epic. Everything
else here exists to make an operation usable as a value: frozen config so one
instance can be reused across many rasters without defensive copying,
`extra="forbid"` so a mistyped field fails at construction instead of being
ignored, and `>>` so two operations compose without either knowing about the
other.

`OperationChain` lives in this module rather than its own, because
`RasterOperation.__rshift__` has to return one and a separate module would make
that a circular import.

`pyramids` is imported only under `TYPE_CHECKING`: an operation is typed against
`Dataset` but never needs the package until something calls `apply`, and
importing the gee backend should not drag the GIS stack in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = [
    "OperationChain",
    "RasterOperation",
]


class RasterOperation(BaseModel, ABC):
    """A configured, callable transform from one raster to another.

    Subclasses declare their configuration as fields on themselves and
    implement `apply`. The instance is the configuration, which is why there is
    no separate config object: the value and the behaviour travel together, as
    `AggregationConfig` and `EarthEngineCredentials` already do elsewhere in
    this package.

    Examples:
        - Define an operation and call it like a function:
            ```python
            >>> class Halve(RasterOperation):
            ...     '''Scale every value by one half.'''
            ...     band: int = 0
            ...
            ...     def apply(self, ds):
            ...         return ds
            >>> op = Halve(band=1)
            >>> op.name
            'Halve'
            >>> op.band
            1

            ```
        - The configuration is frozen, so one instance is safe to reuse:
            ```python
            >>> op.band = 2  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: Instance is frozen

            ```
        - An unknown field is a construction error, not a silent no-op:
            ```python
            >>> Halve(bnad=1)  # doctest: +IGNORE_EXCEPTION_DETAIL
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: Extra inputs are not permitted

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @abstractmethod
    def apply(self, ds: Dataset) -> Dataset:
        """Return a new raster derived from `ds`.

        Args:
            ds: The source raster, already fetched and in memory.

        Returns:
            A new `pyramids.dataset.Dataset`. Implementations do not mutate
            `ds`, so the caller's raster stays usable afterwards.
        """

    def __call__(self, ds: Dataset) -> Dataset:
        """Alias for `apply`, so an operation reads as a function at the call site.

        Args:
            ds: The source raster.

        Returns:
            The derived raster.
        """
        return self.apply(ds)

    @property
    def name(self) -> str:
        """A short label for this operation, used in chain names and errors."""
        return type(self).__name__

    def __rshift__(self, other: RasterOperation) -> OperationChain:
        """Compose with `other`, returning a chain that runs self first.

        Chains flatten rather than nest: `a >> b >> c` is one chain of three,
        not a chain holding a chain, so `name` and iteration stay readable
        however the expression was grouped.

        Args:
            other: The operation to run on this one's output.

        Returns:
            An `OperationChain` of both, left to right.

        Raises:
            TypeError: If `other` is not a `RasterOperation`.
        """
        if not isinstance(other, RasterOperation):
            raise TypeError(
                f"cannot compose {self.name} with {type(other).__name__}: "
                ">> needs a RasterOperation on both sides"
            )
        left = self.operations if isinstance(self, OperationChain) else (self,)
        right = other.operations if isinstance(other, OperationChain) else (other,)
        return OperationChain(operations=(*left, *right))


class OperationChain(RasterOperation):
    """An ordered run of operations, itself an operation.

    Being a `RasterOperation` is the point: anything that accepts one operation
    accepts a chain, so `GEE.analyze` needs no special case for "several".

    Examples:
        - Build a chain with `>>` and read its name:
            ```python
            >>> class Keep(RasterOperation):
            ...     '''Return the raster unchanged.'''
            ...     tag: str = "a"
            ...
            ...     def apply(self, ds):
            ...         return ds
            >>> chain = Keep(tag="first") >> Keep(tag="second")
            >>> chain.name
            'Keep >> Keep'
            >>> len(chain.operations)
            2

            ```
        - Composition flattens, so grouping does not change the result:
            ```python
            >>> left = (Keep() >> Keep()) >> Keep()
            >>> right = Keep() >> (Keep() >> Keep())
            >>> len(left.operations), len(right.operations)
            (3, 3)

            ```
        - `of` takes the operations positionally, for a list built elsewhere:
            ```python
            >>> OperationChain.of(Keep(tag="x"), Keep(tag="y")).name
            'Keep >> Keep'

            ```
    """

    operations: tuple[RasterOperation, ...]

    @classmethod
    def of(cls, *operations: RasterOperation) -> OperationChain:
        """Build a chain from operations given positionally.

        Args:
            *operations: The operations to run, in order.

        Returns:
            A chain over those operations, flattened one level so passing an
            existing chain extends it rather than embedding it.
        """
        flattened: list[RasterOperation] = []
        for operation in operations:
            if isinstance(operation, OperationChain):
                flattened.extend(operation.operations)
            else:
                flattened.append(operation)
        return cls(operations=tuple(flattened))

    def apply(self, ds: Dataset) -> Dataset:
        """Run every operation in order, feeding each the previous result.

        Args:
            ds: The source raster.

        Returns:
            The raster the last operation produced. An empty chain returns `ds`
            unchanged, which keeps `OperationChain.of()` usable as a no-op
            rather than an error the caller has to special-case.
        """
        for operation in self.operations:
            ds = operation.apply(ds)
        return ds

    @property
    def name(self) -> str:
        """The members' names joined with the operator that composed them."""
        return " >> ".join(operation.name for operation in self.operations)

    def __len__(self) -> int:
        """The number of operations in the chain."""
        return len(self.operations)

    def __iter__(self) -> Any:
        """Iterate the operations in run order.

        Returns:
            An iterator over the chain's operations. This shadows pydantic's
            field iteration deliberately: a chain reads as the sequence it
            represents, which is what a caller inspecting one expects.
        """
        return iter(self.operations)
