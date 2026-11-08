"""Tests for the VideoSink frames-collection batch export."""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("ee")

from pydantic import ValidationError  # noqa: E402

from earthlens.gee import jobs as jobs_module  # noqa: E402
from earthlens.gee import video_sink as video_sink_module  # noqa: E402
from earthlens.gee.jobs import get_task_status  # noqa: E402
from earthlens.gee.video_sink import VideoSink  # noqa: E402
from earthlens.gee.work import RasterWorkUnit  # noqa: E402

_WHEN = dt.datetime(2024, 1, 1)
_SENTINEL = object()


class _FakeTask:
    """Records start() and returns a video-task status()."""

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def status(self) -> dict[str, object]:
        return {
            "state": "READY",
            "id": "task-1",
            "task_type": "EXPORT_VIDEO",
            "description": "p",
            "creation_timestamp_ms": 1_704_067_200_000,
            "update_timestamp_ms": 1_704_067_200_000,
        }


class _FakeVideoExport:
    """Stands in for `ee.batch.Export.video`, recording the factory + kwargs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.task = _FakeTask()

    def _record(self, name: str, **kwargs: object) -> _FakeTask:
        self.calls.append((name, kwargs))
        return self.task

    def toDrive(self, **kwargs: object) -> _FakeTask:  # noqa: N802 - EE method name
        return self._record("toDrive", **kwargs)

    def toCloudStorage(self, **kwargs: object) -> _FakeTask:  # noqa: N802
        return self._record("toCloudStorage", **kwargs)


@pytest.fixture
def fake_export(monkeypatch):
    """Patch the EE video-export factory and the TaskInfo/wait seams."""
    export = _FakeVideoExport()
    waited: list[object] = []
    monkeypatch.setattr("ee.batch.Export.video", export, raising=False)
    monkeypatch.setattr(video_sink_module, "_op_to_taskinfo", lambda status: _SENTINEL)
    monkeypatch.setattr(
        video_sink_module, "wait_for_task", lambda task, **_: waited.append(task)
    )
    export.waited = waited
    return export


class TestVideoSinkConfig:
    """The frozen export configuration."""

    def test_defaults(self):
        """A bare sink targets Drive at one frame per second, non-blocking."""
        sink = VideoSink()
        assert (sink.destination, sink.fps, sink.crs, sink.wait_for_export) == (
            "drive",
            1.0,
            "EPSG:4326",
            False,
        )

    def test_it_is_frozen(self):
        """The configuration cannot drift after construction."""
        sink = VideoSink(destination="gcs", bucket="b")
        with pytest.raises(ValidationError):
            sink.bucket = "other"

    def test_a_typo_is_refused(self):
        """An unknown keyword raises instead of silently no-op'ing."""
        with pytest.raises(ValidationError):
            VideoSink(framesPerSecond=5)

    def test_gcs_requires_a_bucket(self):
        """A Cloud Storage export has nowhere to go without a bucket."""
        with pytest.raises(ValidationError, match="bucket"):
            VideoSink(destination="gcs")

    def test_drive_needs_no_folder(self):
        """Earth Engine exports to the Drive root when no folder is given."""
        assert VideoSink(destination="drive").folder is None

    @pytest.mark.parametrize("fps", [0.0, 0.05, 121])
    def test_an_out_of_range_fps_is_refused(self, fps):
        """Earth Engine only accepts 0.1 to 120 frames per second."""
        with pytest.raises(ValidationError, match=r"\[0.1, 120\]"):
            VideoSink(fps=fps)

    @pytest.mark.parametrize("fps", [0.1, 120])
    def test_the_fps_bounds_are_inclusive(self, fps):
        """The documented 0.1 and 120 endpoints are both accepted."""
        assert VideoSink(fps=fps).fps == fps

    @pytest.mark.parametrize("dimensions", ["640x480\n", "640x480 ", " 640x480"])
    def test_a_dimensions_with_trailing_junk_is_refused(self, dimensions):
        """`$` matches before a trailing newline; the validator must not."""
        with pytest.raises(ValidationError, match="WIDTHxHEIGHT"):
            VideoSink(dimensions=dimensions)

    @pytest.mark.parametrize("dimensions", [0, -640, "640x", "x480", "wide"])
    def test_a_malformed_dimensions_is_refused(self, dimensions):
        """dimensions is a positive max dimension or WIDTHxHEIGHT."""
        with pytest.raises(ValidationError, match="WIDTHxHEIGHT"):
            VideoSink(dimensions=dimensions)

    @pytest.mark.parametrize("dimensions", [720, "640x480"])
    def test_a_well_formed_dimensions_is_accepted(self, dimensions):
        """Both the single-int and the WIDTHxHEIGHT forms pass."""
        assert VideoSink(dimensions=dimensions).dimensions == dimensions

    def test_a_non_positive_max_frames_is_refused(self):
        """A frame cap below one caps nothing."""
        with pytest.raises(ValidationError):
            VideoSink(max_frames=0)


class TestVideoSinkWrite:
    """Each destination dispatches to its Export.video factory."""

    def test_drive(self, fake_export):
        """Drive passes the folder, prefix, fps and crs, and returns a TaskInfo."""
        result = VideoSink(destination="drive", folder="exports", fps=5).write(
            "frames", prefix="p"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toDrive"
        assert kwargs["collection"] == "frames"
        assert kwargs["folder"] == "exports"
        assert kwargs["fileNamePrefix"] == "p"
        assert kwargs["description"] == "p"
        assert kwargs["framesPerSecond"] == 5
        assert kwargs["crs"] == "EPSG:4326"
        assert fake_export.task.started is True
        assert result is _SENTINEL

    def test_gcs(self, fake_export):
        """GCS passes the bucket."""
        VideoSink(destination="gcs", bucket="my-bucket").write("frames", prefix="p")
        name, kwargs = fake_export.calls[0]
        assert name == "toCloudStorage"
        assert kwargs["bucket"] == "my-bucket"

    def test_a_long_prefix_is_truncated_for_the_description(self, fake_export):
        """The task description is capped at EE's 100 characters; the prefix is not."""
        prefix = "p" * 120
        VideoSink(destination="drive").write("frames", prefix=prefix)
        _, kwargs = fake_export.calls[0]
        assert kwargs["description"] == "p" * 100
        assert kwargs["fileNamePrefix"] == prefix

    def test_the_optional_knobs_are_omitted_when_unset(self, fake_export):
        """region/scale/dimensions/maxFrames stay off the wire by default."""
        VideoSink(destination="drive").write("frames", prefix="p")
        _, kwargs = fake_export.calls[0]
        assert not {"region", "scale", "dimensions", "maxFrames"} & set(kwargs)

    def test_the_optional_knobs_are_passed_when_set(self, fake_export):
        """region/scale/dimensions/maxFrames reach ee under their wire names."""
        sink = VideoSink(destination="drive", dimensions="640x480", max_frames=500)
        sink.write("frames", prefix="p", region="geom", scale=30)
        _, kwargs = fake_export.calls[0]
        assert kwargs["region"] == "geom"
        assert kwargs["scale"] == 30
        assert kwargs["dimensions"] == "640x480"
        assert kwargs["maxFrames"] == 500

    def test_wait_for_export_blocks(self, fake_export):
        """With wait_for_export the task is waited on before the TaskInfo."""
        VideoSink(destination="drive", wait_for_export=True).write("frames", prefix="p")
        assert fake_export.waited == [fake_export.task]

    def test_no_wait_does_not_block(self, fake_export):
        """Without wait_for_export the task is not waited on."""
        VideoSink(destination="drive").write("frames", prefix="p")
        assert fake_export.waited == []

    def test_export_from_work_unit(self, fake_export):
        """export pulls the frames off a work unit's payload and labels it."""
        unit = RasterWorkUnit("frames", _WHEN, _WHEN, _WHEN)
        result = VideoSink(destination="drive").export(unit, prefix="my-video")
        name, kwargs = fake_export.calls[0]
        assert name == "toDrive"
        assert kwargs["collection"] == "frames"
        assert kwargs["fileNamePrefix"] == "my-video"
        assert result is _SENTINEL

    def test_export_default_prefix_is_the_unit_repr(self, fake_export):
        """Without an explicit prefix, the unit's repr is used."""
        unit = RasterWorkUnit("frames", _WHEN, _WHEN, _WHEN)
        VideoSink(destination="drive").export(unit)
        _, kwargs = fake_export.calls[0]
        assert kwargs["fileNamePrefix"] == repr(unit)


class TestTaskTracking:
    """A queued video export rides the unchanged jobs machinery."""

    def test_the_video_wire_type_is_export_video(self):
        """ee's video tasks carry the EXPORT_VIDEO wire value."""
        import ee.batch

        assert ee.batch.Task.Type.EXPORT_VIDEO.value == "EXPORT_VIDEO"

    def test_write_returns_a_taskinfo_with_the_video_wire_type(self, monkeypatch):
        """The submitted status normalises to a TaskInfo typed EXPORT_VIDEO."""
        export = _FakeVideoExport()
        monkeypatch.setattr("ee.batch.Export.video", export, raising=False)
        info = VideoSink(destination="drive").write("frames", prefix="p")
        assert (info.id, info.state, info.task_type) == (
            "task-1",
            "READY",
            "EXPORT_VIDEO",
        )

    def test_the_queued_task_round_trips_through_get_task_status(self, monkeypatch):
        """The TaskInfo id resolves through jobs.get_task_status."""
        operation = {
            "name": "projects/p/operations/task-1",
            "metadata": {
                "state": "RUNNING",
                "type": "EXPORT_VIDEO",
                "createTime": "2024-01-01T00:00:00Z",
                "updateTime": "2024-01-01T00:01:00Z",
                "description": "p",
            },
            "done": False,
        }
        monkeypatch.setattr(jobs_module.ee.data, "getOperation", lambda name: operation)
        info = get_task_status("task-1", project="p")
        assert (info.id, info.state, info.task_type) == (
            "task-1",
            "RUNNING",
            "EXPORT_VIDEO",
        )
