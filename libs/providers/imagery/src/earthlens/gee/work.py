"""One bucket's payload, and what a download hands back.

`_download_dataset` built a four-tuple per time bucket — `(when, image,
bucket_start, bucket_end)` — and `_api` unpacked it positionally. A tuple cannot
say what kind of thing it carries, so the whole pipeline was implicitly "one
`ee.Image` per bucket": there was nowhere for a vector or tabular result to go,
which is what Epics 3-5 need.

:class:`WorkUnit` makes the payload a typed object. :class:`RasterWorkUnit` is
what every request produces today; :class:`FeatureWorkUnit` exists so a sampler
or zonal result has a path through the same pipeline without a second one being
built beside it.

:class:`Artifact` and :class:`DownloadResult` are the other end: what a bucket
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

from earthlens.base.leaves import SummarisedLeaf

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
        """Return the bucket's half-open `(start, end)` window."""

    def as_tuple(self) -> tuple[dt.datetime, Any, dt.datetime, dt.datetime]:
        """Return the pre-Epic-1 four-tuple this replaced.

        Returns:
            `(when, payload, bucket_start, bucket_end)`.
        """
        return self.when, self.payload, self.bucket_start, self.bucket_end

    def __iter__(self) -> Iterator[Any]:
        """Unpack as the four-tuple `_composite` used to yield.

        Kept so every existing caller — and the doctests and tests that read
        `for when, image, start, end in gee._composite(...)` — keeps working.
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
    """One `ee.Image` — what every request produces today."""

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

    Exactly one of `path`, `destination` or `obj` is set, matching the three
    things a sink can hand back: a file it wrote, a URI it queued work to, or the
    in-memory object it built.

    Attributes:
        kind: The work unit's kind, carried through so a caller can tell a
            raster result from a vector one without inspecting the payload.
        when: The bucket this artifact covers.
        path: The written file, for a synchronous sink.
        destination: The destination URI, for a batch sink that waited.
        task: The submitted `TaskInfo`, for a batch sink that did not wait.
        obj: The in-memory pyramids object, when `materialize` was asked for.
    """

    model_config = SummarisedLeaf.model_config | {"arbitrary_types_allowed": True}

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
        in-memory object when one was asked for.

        Returns:
            The artifact's payload, whichever of the four is set.
        """
        for candidate in (self.obj, self.path, self.destination, self.task):
            if candidate is not None:
                return candidate
        return None


class DownloadResult(SummarisedLeaf):
    """Every artifact one `download()` produced.

    Iterates and indexes like the `list` the method used to return, so a caller
    doing `for path in el.download()` keeps working, while a caller who asked to
    materialise can reach the in-memory objects through :attr:`objects`.

    Attributes:
        artifacts: One artifact per bucket, in the order they were written.
    """

    model_config = SummarisedLeaf.model_config | {"arbitrary_types_allowed": True}

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

        Keeps `assert el.download() == [path]` working in every existing test
        and notebook, which is the contract this type has to preserve.

        Args:
            other: A `DownloadResult`, or a list of payloads.

        Returns:
            Whether the two describe the same payloads.
        """
        if isinstance(other, DownloadResult):
            return self.artifacts == other.artifacts
        if isinstance(other, list):
            return self.values == other
        return NotImplemented

    def __hash__(self) -> int:
        """Hash by the artifacts, since the model is frozen."""
        return hash(self.artifacts)
