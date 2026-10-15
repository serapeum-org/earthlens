"""One bucket's payload, and what a download hands back.

`_download_dataset` built a four-tuple per time bucket — `(when, image,
bucket_start, bucket_end)` — and `_api` unpacked it positionally. A tuple cannot
say what kind of thing it carries, so the whole pipeline was implicitly "one
`ee.Image` per bucket": there was nowhere for a vector or tabular result to go,
which is what Epics 3-5 need.

`WorkUnit` makes the payload a typed object. `RasterWorkUnit` is
what every request produces today; `FeatureWorkUnit` exists so a sampler
or zonal result has a path through the same pipeline without a second one being
built beside it.

`Artifact` and `DownloadResult` are the other end: what a bucket
produced, and the collection of those a `download()` returns. They let a caller
ask for the in-memory pyramids object rather than only a written file, which was
previously impossible — every terminal method wrote to disk and returned a
`Path`, with no seam to return the `Dataset` it had just built.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ConfigDict

from earthlens.base.leaves import SummarisedLeaf
from earthlens.base.raster import close_quietly

__all__ = [
    "WorkUnit",
    "RasterWorkUnit",
    "FeatureWorkUnit",
    "Artifact",
    "DownloadResult",
]


class WorkUnit(ABC):
    """One time bucket's payload, with the window it was composited over.

    Attributes:
        kind: What this unit carries, matching the `OUTPUT_KIND` vocabulary so
            a backend can report its output shape from the units it produced.
    """

    kind: ClassVar[str]

    def __init__(
        self,
        payload: Any,
        when: dt.datetime,
        bucket_start: dt.datetime,
        bucket_end: dt.datetime,
    ) -> None:
        """Bind a payload to the bucket it belongs to.

        Args:
            payload: The Earth Engine object this bucket reduced to.
            when: The bucket start, which the output filename is derived from.
            bucket_start: The bucket's inclusive start.
            bucket_end: The bucket's exclusive end.
        """
        self.payload = payload
        self.when = when
        self.bucket_start = bucket_start
        self.bucket_end = bucket_end

    @property
    @abstractmethod
    def window(self) -> tuple[dt.datetime, dt.datetime]:
        """Return the bucket's half-open `(start, end)` window.

        Returns:
            The inclusive start and exclusive end the payload covers. A
            subclass whose payload spans something other than its own bucket
            overrides this to say so.
        """

    def as_tuple(self) -> tuple[dt.datetime, Any, dt.datetime, dt.datetime]:
        """Return the pre-Epic-1 four-tuple this replaced.

        Returns:
            `(when, payload, bucket_start, bucket_end)`.
        """
        return self.when, self.payload, self.bucket_start, self.bucket_end

    def __iter__(self) -> Iterator[Any]:
        """Unpack as the four-tuple `_composite` used to yield.

        Kept so a caller written as
        `for when, image, start, end in gee._composite(...)` keeps working.
        New code should read the named attributes.

        Returns:
            An iterator over `(when, payload, bucket_start, bucket_end)`.
        """
        return iter(self.as_tuple())

    def __getitem__(self, index: int) -> Any:
        """Index as the four-tuple, for the same back-compat reason.

        Args:
            index: Position in `(when, payload, bucket_start, bucket_end)`.

        Returns:
            That element.

        Raises:
            IndexError: If `index` is outside the four positions.
        """
        return self.as_tuple()[index]

    def __len__(self) -> int:
        """Return 4, the length of the tuple this stands in for."""
        return 4

    def __repr__(self) -> str:
        """Name the kind and the bucket, for log lines and failures."""
        return f"{type(self).__name__}({self.when:%Y-%m-%d})"


class _DatedWorkUnit(WorkUnit):
    """A work unit whose window is simply the bucket it was built for."""

    @property
    def window(self) -> tuple[dt.datetime, dt.datetime]:
        """Return the bucket's half-open `(start, end)` window.

        Returns:
            The inclusive start and exclusive end the reader re-composites over.
        """
        return self.bucket_start, self.bucket_end


class RasterWorkUnit(_DatedWorkUnit):
    """One `ee.Image` — what every request produces today.

    Examples:
        - The payload is reachable under its domain name, with the bucket it
          covers:
            ```python
            >>> import datetime as dt
            >>> unit = RasterWorkUnit(
            ...     "an ee.Image",
            ...     dt.datetime(2024, 1, 1),
            ...     dt.datetime(2024, 1, 1),
            ...     dt.datetime(2024, 2, 1),
            ... )
            >>> unit.kind, unit.image
            ('raster', 'an ee.Image')
            >>> start, end = unit.window
            >>> start.date(), end.date()
            (datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))

            ```
        - It still unpacks as the four-tuple the pipeline used to pass around:
            ```python
            >>> import datetime as dt
            >>> unit = RasterWorkUnit(
            ...     "an ee.Image",
            ...     dt.datetime(2024, 1, 1),
            ...     dt.datetime(2024, 1, 1),
            ...     dt.datetime(2024, 2, 1),
            ... )
            >>> when, image, bucket_start, bucket_end = unit
            >>> when.date(), image
            (datetime.date(2024, 1, 1), 'an ee.Image')

            ```
    """

    kind: ClassVar[str] = "raster"

    @property
    def image(self) -> Any:
        """Return the payload under its domain name.

        Returns:
            The `ee.Image` this bucket reduced to.
        """
        return self.payload


class FeatureWorkUnit(_DatedWorkUnit):
    """One `ee.FeatureCollection` — the path Epics 3-5 emit through.

    Nothing produces one yet. It exists so a sampler or zonal result has a route
    through the same pipeline rather than needing a parallel one, which is the
    whole reason the payload became a type.
    """

    kind: ClassVar[str] = "vector"

    @property
    def features(self) -> Any:
        """Return the payload under its domain name.

        Returns:
            The `ee.FeatureCollection` this bucket reduced to.
        """
        return self.payload


class Artifact(SummarisedLeaf):
    """What one bucket produced, written or in memory.

    A sink populates the fields that describe what it handed back: a file it
    wrote, a destination URI or submitted task for work it queued, and — when
    `materialize` was asked for — the in-memory object read back from the file.
    `value` collapses that to the single thing the pre-Epic-1 contract
    returned.

    Attributes:
        kind: The work unit's kind, carried through so a caller can tell a
            raster result from a vector one without inspecting the payload.
        when: The bucket this artifact covers.
        path: The written file, for a synchronous sink.
        destination: The destination URI, for a batch sink that waited.
        task: The submitted `TaskInfo`, for a batch sink that did not wait.
        obj: The in-memory pyramids object, when `materialize` was asked for.
    """

    # Spelled out rather than derived from `SummarisedLeaf.model_config`: that
    # base declares no config, so the `|` inherited pydantic's permissive
    # defaults and quietly dropped both `frozen` and `extra`.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    _summary_fields: ClassVar[tuple[str, ...]] = ("kind", "when", "path", "destination")

    kind: str = "raster"
    when: dt.datetime | None = None
    path: Path | None = None
    destination: str | None = None
    task: Any = None
    obj: Any = None

    @property
    def value(self) -> Any:
        """Return the one thing this artifact actually carries.

        Preserves the pre-Epic-1 return contract: a `Path` for the synchronous
        sink, a destination string or `TaskInfo` for a batch sink, and the
        in-memory object when one was asked for. The first of `obj`, `path`,
        `destination`, `task` that is set wins, so a materialised bucket — which
        carries both the object and the file it was read back from — yields the
        object.

        Returns:
            The artifact's payload, or `None` when the artifact carries nothing.

        Examples:
            - A written file reads back as its path:
                ```python
                >>> from pathlib import Path
                >>> Artifact(path=Path("chirps_precip_20240101.tif")).value.name
                'chirps_precip_20240101.tif'

                ```
            - A queued export reads back as the destination it will appear at:
                ```python
                >>> Artifact(destination="drive://exports/era5_t2m").value
                'drive://exports/era5_t2m'

                ```
        """
        for candidate in (self.obj, self.path, self.destination, self.task):
            if candidate is not None:
                return candidate
        return None


class DownloadResult(SummarisedLeaf):
    """Every artifact one `download()` produced.

    Iterates and indexes like the `list` the method used to return, so a caller
    doing `for path in el.download()` keeps working, while a caller who asked to
    materialise can reach the in-memory objects through `objects`.

    Attributes:
        artifacts: One artifact per bucket, in the order they were written.

    Examples:
        - It stands in for the list of written paths, and compares equal to
          one:
            ```python
            >>> from pathlib import Path
            >>> result = DownloadResult(
            ...     artifacts=(Artifact(path=Path("era5_t2m_20240101.tif")),)
            ... )
            >>> len(result)
            1
            >>> result == [Path("era5_t2m_20240101.tif")]
            True

            ```
        - The written files are reachable as paths whatever else a bucket
          carried:
            ```python
            >>> from pathlib import Path
            >>> result = DownloadResult(
            ...     artifacts=(
            ...         Artifact(path=Path("a.tif")),
            ...         Artifact(destination="gs://bucket/b"),
            ...     )
            ... )
            >>> [path.name for path in result.paths]
            ['a.tif']
            >>> result.values[1]
            'gs://bucket/b'

            ```
    """

    # Spelled out rather than derived from `SummarisedLeaf.model_config`: that
    # base declares no config, so the `|` inherited pydantic's permissive
    # defaults and quietly dropped both `frozen` and `extra`.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    _summary_fields: ClassVar[tuple[str, ...]] = ("artifacts",)

    artifacts: tuple[Artifact, ...] = ()

    @property
    def values(self) -> list[Any]:
        """Return each artifact's payload, the pre-Epic-1 `list` return.

        Returns:
            One value per bucket.
        """
        return [artifact.value for artifact in self.artifacts]

    @property
    def paths(self) -> list[Path]:
        """Return only the written files.

        Returns:
            The `Path` of every artifact that wrote one.
        """
        return [a.path for a in self.artifacts if a.path is not None]

    @property
    def objects(self) -> list[Any]:
        """Return only the in-memory objects.

        Returns:
            The pyramids object of every artifact that carries one.
        """
        return [a.obj for a in self.artifacts if a.obj is not None]

    def collection(self, **kwargs: Any) -> Any:
        """Stack the written rasters into a pyramids `DatasetCollection`.

        A multi-bucket download is a time series, and pyramids already has the
        type for one, so the result should hand it over rather than leaving the
        caller to glob the directory back. Built from the written files through
        `DatasetCollection.from_files`, which is pyramids' own constructor for
        exactly this - there is no public API for stacking in-memory datasets,
        and the files are on disk either way.

        The default date pattern is matched to this backend's filenames
        (`<asset>_<bands>_<YYYYMMDD>.tif`), whose dates carry no separators;
        pass `date_regex=` / `date_format=` to override for files named by
        something else.

        Args:
            **kwargs: Forwarded to `DatasetCollection.from_files`, overriding
                the date pattern above when given.

        Returns:
            The pyramids `DatasetCollection` over this result's paths.

        Raises:
            ValueError: If no bucket wrote a file - a batch export returns
                destinations, so there is nothing local to stack.

        Examples:
            - A result with no written files says so, and says what to do
              instead, rather than handing back an empty collection:
                ```python
                >>> from earthlens.gee.work import Artifact, DownloadResult
                >>> queued = DownloadResult(
                ...     artifacts=(Artifact(destination="gs://my-bucket/srtm"),)
                ... )
                >>> queued.paths
                []
                >>> queued.collection()  # doctest: +ELLIPSIS
                Traceback (most recent call last):
                    ...
                ValueError: this result holds no written files, ...

                ```
        """
        from pyramids.dataset import DatasetCollection

        paths = self.paths
        if not paths:
            raise ValueError(
                "this result holds no written files, so there is nothing to "
                "stack; a batch export returns destinations - pull them first, "
                "then DatasetCollection.from_files(...) over the pulled files."
            )
        options: dict[str, Any] = {"date_regex": r"\d{8}", "date_format": "%Y%m%d"}
        options.update(kwargs)
        return DatasetCollection.from_files([str(path) for path in paths], **options)

    def close(self) -> None:
        """Close every in-memory object this result holds.

        Materialising hands back live pyramids objects, and each one keeps a
        GDAL handle open. On Windows that handle locks the file, so a later
        write or rename of the same path fails with `PermissionError` until it
        is released — and a `gc.collect()` cannot free it while the caller still
        holds the result. Call this, or use the result as a context manager,
        once the objects are no longer needed.

        Idempotent, and never raises: an already-closed handle, or a value that
        is not a dataset, is skipped.
        """
        for artifact in self.artifacts:
            close_quietly(artifact.obj)

    def __enter__(self) -> DownloadResult:
        """Return the result, so it can be used as a context manager.

        Returns:
            This result.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close every held object on the way out.

        Args:
            *exc_info: The exception triple, ignored — closing happens either
                way.
        """
        self.close()

    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        """Iterate the payloads, so it stands in for the old `list` return.

        Returns:
            An iterator over each artifact's value.
        """
        return iter(self.values)

    def __len__(self) -> int:
        """Return how many buckets were produced."""
        return len(self.artifacts)

    def __getitem__(self, index: int) -> Any:
        """Return one bucket's payload by position.

        Args:
            index: The bucket position.

        Returns:
            That artifact's value.
        """
        return self.values[index]

    def __eq__(self, other: object) -> bool:
        """Compare equal to the plain list of payloads, as well as to a peer.

        `download()` used to return that list, so code and notebooks written
        against it compare the result straight to `[path]`. Keeping that true is
        the contract this type has to preserve.

        Args:
            other: A `DownloadResult`, or a list of payloads.

        Returns:
            Whether the two describe the same payloads, or `NotImplemented` for
            anything else — which leaves the answer to the other operand rather
            than claiming inequality.
        """
        if isinstance(other, DownloadResult):
            # By payload, not by artifact. Comparing artifacts made equality
            # intransitive: two results could each equal the same plain list
            # while differing from one another, because one carried a path and
            # the other the object read back from it.
            return self.values == other.values
        if isinstance(other, list):
            return self.values == other
        return NotImplemented

    def __hash__(self) -> int:
        """Hash consistently with `__eq__`, which compares payloads.

        Deliberately coarse. Equality is by payload, and a materialised payload
        is a GDAL-backed object that need not be hashable, so the payloads
        themselves cannot go into the hash. The count can: equal results always
        agree on it, which is what `__hash__` has to guarantee.

        Returns:
            A hash of the number of artifacts.
        """
        return hash(len(self.artifacts))
