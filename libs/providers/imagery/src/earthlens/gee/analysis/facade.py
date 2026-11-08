"""`RasterAnalysis`: run a chain over what a download returned.

The operations are usable on their own - each one takes a `Dataset` and returns
one - so this adds only the part that knows about Epic 1's result type: pull the
in-memory object out of a `DownloadResult`, run the chain on it, and either hand
back the derived raster or write it next to the download.

`GEE.analyze(ops)` is a one-line delegation to this class, so the backend gains
no analysis logic of its own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict, field_validator

from earthlens.gee.analysis.base import OperationChain, RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

    from earthlens.gee.work import Artifact, DownloadResult

__all__ = ["RasterAnalysis"]


def _slug(name: str) -> str:
    """Turn a chain's name into something safe for a file name.

    Args:
        name: A chain name such as `Ndvi >> Reclassify`.

    Returns:
        A lowercase, underscore-joined form, capped so a long chain cannot
        push a path past a file-system limit.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()
    return (cleaned or "analysis")[:60]


def _materialised(result: DownloadResult) -> list[Artifact]:
    """Return the artifacts that carry an in-memory raster.

    Separate from `run_on_result` so the diagnosis of a wrong argument lives in
    one place. The previous version read `getattr(result, "objects", [])`, which
    accepted anything with that attribute and mapped every other mistake to the
    same `materialize=True` advice - including the two most likely ones, a list
    of rasters and a single raster, for neither of which that advice is true.

    Args:
        result: What the caller passed as a download result.

    Returns:
        The artifacts whose `obj` is set, in download order.

    Raises:
        TypeError: If `result` is not a `DownloadResult`.
        ValueError: If it is one but nothing in it was materialised.
    """
    from pyramids.dataset import Dataset as PyramidsDataset

    if isinstance(result, PyramidsDataset):
        raise TypeError(
            "run_on_result takes the DownloadResult, but this is a single "
            "raster; call run(raster) (or the analysis itself) instead"
        )
    if isinstance(result, (list, tuple)):
        raise TypeError(
            "run_on_result takes the DownloadResult, not a sequence of "
            f"{len(result)} item(s); either pass the result itself, or run the "
            "analysis over the sequence yourself with "
            "[analysis.run(raster) for raster in rasters]"
        )
    artifacts = getattr(result, "artifacts", None)
    if artifacts is None:
        raise TypeError(
            f"run_on_result expected a DownloadResult, got "
            f"{type(result).__name__}, which carries no artifacts"
        )
    materialised = [artifact for artifact in artifacts if artifact.obj is not None]
    if not materialised:
        raise ValueError(
            f"none of this download's {len(artifacts)} artifact(s) carries an "
            "in-memory raster to analyse; call download with materialize=True "
            "so the result keeps the pyramids object alongside the written file"
        )
    return materialised


class RasterAnalysis(OperationChain):
    """A chain plus the knowledge of how to feed it a download result.

    Being an `OperationChain` means the fold, the name and the length come from
    one implementation rather than a second copy of it, that
    `RasterAnalysis(...)` can be dropped into another chain, and that `run` is
    just `apply` under a name that reads better at a call site. What this adds
    is `of`'s tolerance of a sequence, and `run_on_result`.

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

    @field_validator("operations", mode="before")
    @classmethod
    def _accept_one_or_many(cls, value: Any) -> Any:
        """Accept a single operation, a chain, or any sequence of them.

        Args:
            value: Whatever the caller passed as `operations`.

        Returns:
            A tuple of operations, with chains flattened one level so a chain
            handed in extends the run rather than nesting inside it.

        Raises:
            ValueError: If `value` is neither an operation nor iterable - an
                `int` or `float` would otherwise reach the loop below and raise
                a bare `TypeError: 'int' object is not iterable`, which pydantic
                does not fold into the `ValidationError` the caller expects
                (and which, being a `ValueError` subclass, matches this
                operation's documented contract).
        """
        if isinstance(value, RasterOperation):
            value = (value,)
        try:
            candidates = list(value)
        except TypeError:
            raise ValueError(
                f"operations must be a RasterOperation, a chain, or a sequence "
                f"of them; got {type(value).__name__}, which is neither"
            ) from None
        flattened: list[RasterOperation] = []
        for operation in candidates:
            # One `isinstance` covers both, now that an analysis *is* a chain.
            if isinstance(operation, OperationChain):
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
        # A single sequence argument is passed through as-is; otherwise the
        # positional operations are. Either way the `_accept_one_or_many`
        # before-validator normalises the value (and raises its own ValueError
        # for anything unusable), so the pre-validation type is deliberately
        # dynamic.
        candidate: Any = (
            operations[0]
            if len(operations) == 1 and not isinstance(operations[0], RasterOperation)
            else operations
        )
        return cls(operations=candidate)

    def run(self, ds: Dataset) -> Dataset:
        """Alias for `apply`, which reads better where an analysis is invoked.

        Args:
            ds: The source raster.

        Returns:
            The derived raster.
        """
        return self.apply(ds)

    def run_on_result(
        self,
        result: DownloadResult,
        *,
        write_to: Path | str | None = None,
        overwrite: bool = False,
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
            overwrite: Whether an existing output file may be replaced. False
                by default, so a re-run into the same directory reports the
                collision rather than quietly discarding the earlier result.

        Returns:
            One derived `Dataset` per in-memory object, or one `Path` each when
            `write_to` is given.

        Raises:
            TypeError: If `result` is not a `DownloadResult` - a bare raster or
                a list of them is a different call, and the message says which.
            ValueError: If the result carries no in-memory object, which means
                the download was made without `materialize=True`.
            FileExistsError: If an output file exists and `overwrite` is False.
        """
        artifacts = _materialised(result)
        derived = [(artifact, self.apply(artifact.obj)) for artifact in artifacts]
        if write_to is None:
            return [dataset for _, dataset in derived]
        directory = Path(write_to)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Dataset | Path] = []
        for index, (artifact, dataset) in enumerate(derived):
            target = directory / self._output_name(artifact, index)
            if target.exists() and not overwrite:
                raise FileExistsError(
                    f"{target} already exists; pass overwrite=True to replace "
                    "it, or write into a fresh directory"
                )
            dataset.to_file(target)
            written.append(target)
        return written

    def _output_name(self, artifact: Artifact, index: int) -> str:
        """Name an output after its source, not after its position.

        `analysis_0.tif ... analysis_N.tif` tied a result to nothing: a
        multi-bucket download gave no way to tell which file came from which
        granule or time bucket, and a second run over the same directory
        overwrote the first. Every other write path in this backend derives a
        descriptive name, so this one does too, preferring the source file's
        own stem and falling back to the bucket's timestamp.

        Args:
            artifact: The artifact the derived raster came from.
            index: Its position in the result, used only as a last resort.

        Returns:
            A file name ending in `.tif`.
        """
        suffix = _slug(self.name)
        if artifact.path is not None:
            return f"{Path(artifact.path).stem}_{suffix}.tif"
        if artifact.when is not None:
            return f"{artifact.when:%Y%m%dT%H%M%S}_{suffix}.tif"
        return f"analysis_{index}_{suffix}.tif"
