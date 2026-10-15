"""`RasterAnalysis`: run a chain over what a download returned.

The operations are usable on their own - each one takes a `Dataset` and returns
one - so this adds only the part that knows about Epic 1's result type: pull the
in-memory object out of a `DownloadResult`, run the chain on it, and either hand
back the derived raster or write it next to the download.

`GEE.analyze(ops)` is a one-line delegation to this class, so the backend gains
no analysis logic of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, field_validator

from earthlens.gee.analysis.base import OperationChain, RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["RasterAnalysis"]


class RasterAnalysis(RasterOperation):
    """A chain plus the knowledge of how to feed it a download result.

    Being a `RasterOperation` itself means `RasterAnalysis(...)` can be dropped
    into another chain, and that `run` is just `apply` under a name that reads
    better at a call site.

    Examples:
        - Run two operations over a raster:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> from earthlens.gee.analysis import Ndvi, Reclassify
            >>> stack = np.stack([
            ...     np.full((2, 2), 0.6, dtype="float32"),
            ...     np.full((2, 2), 0.2, dtype="float32"),
            ... ])
            >>> raster = Dataset.from_array(
            ...     arr=stack,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=np.nan,
            ... )
            >>> analysis = RasterAnalysis.of(
            ...     Ndvi(nir=0, red=1),
            ...     Reclassify(bins=((0.3, 1.0, 1),)),
            ... )
            >>> float(np.asarray(analysis.run(raster).read_array())[0, 0])
            1.0
            >>> analysis.name
            'Ndvi >> Reclassify'

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    operations: tuple[RasterOperation, ...]

    @field_validator("operations", mode="before")
    @classmethod
    def _accept_one_or_many(cls, value: Any) -> Any:
        """Accept a single operation, a chain, or any sequence of them.

        Args:
            value: Whatever the caller passed as `operations`.

        Returns:
            A tuple of operations, with chains flattened one level so a chain
            handed in extends the run rather than nesting inside it.
        """
        if isinstance(value, RasterOperation):
            value = (value,)
        flattened: list[RasterOperation] = []
        for operation in value:
            if isinstance(operation, OperationChain | RasterAnalysis):
                flattened.extend(operation.operations)
            else:
                flattened.append(operation)
        return tuple(flattened)

    @classmethod
    def of(
        cls, *operations: RasterOperation | Sequence[RasterOperation]
    ) -> RasterAnalysis:
        """Build an analysis from operations given positionally or as a sequence.

        Args:
            *operations: Operations, chains, or one sequence of them.

        Returns:
            The analysis.
        """
        if len(operations) == 1 and not isinstance(operations[0], RasterOperation):
            return cls(operations=operations[0])
        return cls(operations=operations)

    @property
    def name(self) -> str:
        """The operations' names joined, as a chain reports them."""
        return " >> ".join(operation.name for operation in self.operations)

    def apply(self, ds: Dataset) -> Dataset:
        """Run every operation in order.

        Args:
            ds: The source raster.

        Returns:
            The raster the last operation produced; `ds` itself when the
            analysis is empty.
        """
        for operation in self.operations:
            ds = operation.apply(ds)
        return ds

    def run(self, ds: Dataset) -> Dataset:
        """Alias for `apply`, which reads better where an analysis is invoked.

        Args:
            ds: The source raster.

        Returns:
            The derived raster.
        """
        return self.apply(ds)

    def run_on_result(
        self, result: Any, *, write_to: Path | str | None = None
    ) -> list[Dataset | Path]:
        """Run the analysis over every in-memory object a download produced.

        Epic 1's `download(materialize=True)` returns a `DownloadResult` whose
        `objects` property holds the pyramids object of each artifact that
        carries one. A download made without `materialize` holds paths only, so
        there is nothing in memory to analyse - that is an error worth naming,
        rather than an empty list the caller has to puzzle over.

        Args:
            result: A `DownloadResult` from `GEE.download`.
            write_to: When given, a directory; each derived raster is written
                there as a GeoTIFF and its path returned instead of the object.

        Returns:
            One derived `Dataset` per in-memory object, or one `Path` each when
            `write_to` is given.

        Raises:
            ValueError: If the result carries no in-memory object, which means
                the download was made without `materialize=True`.
        """
        objects = list(getattr(result, "objects", []) or [])
        if not objects:
            raise ValueError(
                "this download carries no in-memory raster to analyse; call "
                "download with materialize=True so the result keeps the "
                "pyramids object alongside the written file"
            )
        derived = [self.apply(obj) for obj in objects]
        if write_to is None:
            return list(derived)
        directory = Path(write_to)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Dataset | Path] = []
        for index, dataset in enumerate(derived):
            target = directory / f"analysis_{index}.tif"
            dataset.to_file(str(target))
            written.append(target)
        return written
