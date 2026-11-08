"""`VideoSink`: export an `ee.ImageCollection` of frames as a batch video task.

The image sinks in `earthlens.gee.sinks` write a composited `ee.Image`; their
`write` is image-shaped, so a video export — which takes an `ee.ImageCollection`
of RGB frames plus `framesPerSecond` / `dimensions` — does not fit that
contract. Like `earthlens.gee.table_sink.TableSink`, `VideoSink` is therefore a
standalone frozen model, not a `sinks.Sink` subclass: it dispatches the frames
to `ee.batch.Export.video.toDrive` / `toCloudStorage` (the only two video
targets Earth Engine offers) and tracks the queued task through the same
`earthlens.gee.jobs` machinery (`TaskInfo`, `wait_for_task_id`,
`resolve_destination`) every other export uses. The wire task type is
`EXPORT_VIDEO`.

Nothing in the download pipeline produces a frames collection yet (the Epic-5
`Timelapse` pairing moved to digitalearth), so the caller provides the
`ee.ImageCollection` — typically built through `GEE.client`, with every frame
an 8-bit RGB visualisation (`ee.Image.visualize`); Earth Engine refuses
non-RGB frames. `export` accepts any `WorkUnit` whose payload is such a
collection, so a future frames-producing pipeline plugs in without a new seam.

Batch exports are Earth Engine-only: the EEDAI fast path reads stored pixels
synchronously and never applies to an asynchronous `ee.batch` export, so there
is no engine choice here and nothing for the reader to decline.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from earthlens.gee._helpers import NoBool, wait_for_task
from earthlens.gee.jobs import TaskInfo, _op_to_taskinfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee

    from earthlens.gee.work import WorkUnit

__all__ = ["VideoSink"]

#: The `"WIDTHxHEIGHT"` form Earth Engine accepts for `dimensions`, e.g.
#: `"640x480"` — two positive integers joined by a literal `x`. Applied with
#: `fullmatch`: under `match`, the `$` also matches just before a trailing
#: newline, so `"640x480\n"` would pass and reach the wire.
_DIMENSIONS_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")


class VideoSink(BaseModel):
    """Queue an `ee.ImageCollection` of frames as a tracked video export.

    A frozen value object holding the destination and the video encoding
    knobs. `export` (from a `WorkUnit` carrying the frames) or `write` (from a
    raw collection) dispatches to `ee.batch.Export.video.toDrive` /
    `toCloudStorage`, starts the task, and returns the `TaskInfo` the rest of
    `earthlens.gee.jobs` tracks by id — its `task_type` is the `EXPORT_VIDEO`
    wire value. With `wait_for_export=True` it blocks until the task reaches a
    terminal state first.

    A Drive export with no `folder` lands in the Drive root (Earth Engine's
    own default); a `gcs` export is refused at construction without its
    `bucket`.

    The encoding knobs are validated here rather than on the wire. A string
    `dimensions` must be exactly `"WIDTHxHEIGHT"` — the pattern is applied to
    the whole value, so `"640x480\\n"` is refused instead of travelling to
    Earth Engine with the newline attached — and `fps`, `dimensions` and
    `max_frames` refuse a `bool`: `bool` is a subclass of `int`, so
    `dimensions=True` would otherwise coerce to `1` and queue a one-pixel
    video.

    Examples:
        - A sink is a frozen value object, constructed without Earth Engine:
            ```python
            >>> from earthlens.gee.video_sink import VideoSink
            >>> sink = VideoSink(destination="gcs", bucket="my-bucket", fps=12)
            >>> (sink.destination, sink.bucket, sink.fps, sink.wait_for_export)
            ('gcs', 'my-bucket', 12.0, False)

            ```
        - Its configuration cannot drift after construction:
            ```python
            >>> from earthlens.gee.video_sink import VideoSink
            >>> sink = VideoSink(destination="drive", folder="videos")
            >>> sink.folder = "elsewhere"
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - A Cloud Storage export without a bucket is refused up front:
            ```python
            >>> from earthlens.gee.video_sink import VideoSink
            >>> VideoSink(destination="gcs")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - `dimensions` takes a positive maximum or the exact
          `"WIDTHxHEIGHT"` form, trailing whitespace included in the
          judgement:
            ```python
            >>> from earthlens.gee.video_sink import VideoSink
            >>> VideoSink(dimensions="640x480").dimensions
            '640x480'
            >>> VideoSink(dimensions="640x480\\n")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - A `bool` is refused where a number is wanted, rather than becoming
          a one-pixel video:
            ```python
            >>> from earthlens.gee.video_sink import VideoSink
            >>> VideoSink(dimensions=True)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: Literal["drive", "gcs"] = "drive"  # Export.video.to* factory
    folder: str | None = None  # Drive folder (drive); None is the Drive root
    bucket: str | None = None  # Cloud Storage bucket (gcs)
    fps: Annotated[float, NoBool] = 1.0  # framesPerSecond; EE accepts 0.1-120
    dimensions: Annotated[int | str | None, NoBool] = (
        None  # max dimension, or "WIDTHxHEIGHT"
    )
    max_frames: Annotated[int | None, NoBool] = Field(
        default=None, gt=0
    )  # maxFrames cap
    crs: str = "EPSG:4326"  # projection of the exported video
    wait_for_export: bool = False  # block until the task finishes

    @field_validator("fps")
    @classmethod
    def _framed(cls, value: float) -> float:
        """Refuse a frame rate outside Earth Engine's documented range.

        Args:
            value: The parsed `fps` input.

        Returns:
            The value unchanged when it is in range.

        Raises:
            ValueError: If `value` is outside `[0.1, 120]`, the
                `framesPerSecond` range `ee.batch.Export.video` documents.
        """
        if not 0.1 <= value <= 120:
            raise ValueError(f"fps must be in [0.1, 120], got {value!r}")
        return value

    @field_validator("dimensions")
    @classmethod
    def _sized(cls, value: int | str | None) -> int | str | None:
        """Refuse a malformed `dimensions` value.

        Args:
            value: The parsed `dimensions` input.

        Returns:
            The value unchanged when it is a positive integer, a
            `"WIDTHxHEIGHT"` string, or `None`.

        Raises:
            ValueError: If an integer is not positive, or a string is not of
                the `"WIDTHxHEIGHT"` form.
        """
        bad = (isinstance(value, int) and value <= 0) or (
            isinstance(value, str) and _DIMENSIONS_RE.fullmatch(value) is None
        )
        if bad:
            raise ValueError(
                "dimensions must be a positive max dimension or "
                f'"WIDTHxHEIGHT", got {value!r}'
            )
        return value

    @model_validator(mode="after")
    def _addressed(self) -> VideoSink:
        """Require the one address the destination cannot run without.

        Drive is deliberately exempt: Earth Engine exports to the Drive root
        when no folder is given.

        Returns:
            The config unchanged once it is consistent.

        Raises:
            ValueError: If `destination="gcs"` was given without `bucket`.
        """
        if self.destination == "gcs" and self.bucket is None:
            raise ValueError(
                'destination "gcs" requires bucket= (the Cloud Storage bucket)'
            )
        return self

    def export(
        self,
        work_unit: WorkUnit,
        *,
        prefix: str | None = None,
        region: Any = None,
        scale: float | None = None,
    ) -> TaskInfo:
        """Export a work unit's frames collection as a tracked video task.

        Args:
            work_unit: A `WorkUnit` whose payload is the `ee.ImageCollection`
                of RGB frames. Nothing in the pipeline produces one yet, so
                this is the seam a future frames producer plugs into.
            prefix: The output filename prefix (and, truncated to Earth
                Engine's 100 characters, the task description); defaults to
                the unit's `repr`.
            region: The `ee.Geometry` to render; defaults to the first
                frame's own region.
            scale: Metres per pixel; an alternative to `dimensions`.

        Returns:
            The `TaskInfo` for the queued (or completed) export.
        """
        label = prefix if prefix is not None else repr(work_unit)
        return self.write(work_unit.payload, prefix=label, region=region, scale=scale)

    def write(
        self,
        frames: ee.ImageCollection,
        *,
        prefix: str,
        region: Any = None,
        scale: float | None = None,
    ) -> TaskInfo:
        """Queue `frames` to this destination and return its `TaskInfo`.

        Args:
            frames: The `ee.ImageCollection` of RGB frames to encode, in
                collection order — Earth Engine refuses non-RGB frames, so
                visualise each one (`ee.Image.visualize`) first.
            prefix: The output filename prefix (and, truncated to Earth
                Engine's 100 characters, the task description).
            region: The `ee.Geometry` to render; defaults to the first
                frame's own region.
            scale: Metres per pixel; an alternative to `dimensions`.

        Returns:
            The `TaskInfo` captured at submission — `task_type` is
            `EXPORT_VIDEO` — or after the task reaches a terminal state when
            `wait_for_export` is `True`.
        """
        import ee

        common: dict[str, Any] = {
            "collection": frames,
            "description": prefix[:100],
            "fileNamePrefix": prefix,
            "framesPerSecond": self.fps,
            "crs": self.crs,
        }
        if region is not None:
            common["region"] = region
        if scale is not None:
            common["scale"] = scale
        if self.dimensions is not None:
            common["dimensions"] = self.dimensions
        if self.max_frames is not None:
            common["maxFrames"] = self.max_frames
        factory = ee.batch.Export.video
        if self.destination == "drive":
            task = factory.toDrive(folder=self.folder, **common)
        else:  # "gcs"
            task = factory.toCloudStorage(bucket=self.bucket, **common)
        task.start()
        if self.wait_for_export:
            wait_for_task(task, progress_bar=False)
        return _op_to_taskinfo(task.status())
