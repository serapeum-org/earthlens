"""Tests for the work units and the download-result contract."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.gee.work import (
    Artifact,
    DownloadResult,
    FeatureWorkUnit,
    RasterWorkUnit,
    WorkUnit,
)

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures

WHEN = dt.datetime(2020, 6, 1)
END = dt.datetime(2020, 7, 1)


class TestWorkUnits:
    """The payload is a type now, so it can say what it carries."""

    def test_a_raster_unit_names_its_payload(self):
        """`image` reads better than `payload` at a raster call site."""
        unit = RasterWorkUnit("the-image", WHEN, WHEN, END)
        assert unit.image == "the-image"
        assert unit.kind == "raster"

    def test_a_feature_unit_names_its_payload(self):
        """The route Epics 3-5 emit through."""
        unit = FeatureWorkUnit("the-fc", WHEN, WHEN, END)
        assert unit.features == "the-fc"
        assert unit.kind == "vector"

    def test_the_two_kinds_differ(self):
        """Which is the whole reason the payload became a type."""
        assert RasterWorkUnit.kind != FeatureWorkUnit.kind

    def test_both_are_work_units(self):
        """Each implements the ABC."""
        for cls in (RasterWorkUnit, FeatureWorkUnit):
            assert issubclass(cls, WorkUnit)

    def test_window_is_the_half_open_bucket(self):
        """The window the reader re-composites over."""
        assert RasterWorkUnit("x", WHEN, WHEN, END).window == (WHEN, END)

    def test_repr_names_the_kind_and_bucket(self):
        """For a log line or a failure message."""
        assert (
            repr(RasterWorkUnit("x", WHEN, WHEN, END)) == "RasterWorkUnit(2020-06-01)"
        )

    def test_a_feature_unit_reads_the_same_way(self):
        """Both kinds share the repr, so a vector failure is as diagnosable."""
        assert (
            repr(FeatureWorkUnit("x", WHEN, WHEN, END)) == "FeatureWorkUnit(2020-06-01)"
        )

    def test_the_base_class_cannot_be_instantiated(self):
        """`window` is abstract, so a bare `WorkUnit` carries no window."""
        with pytest.raises(TypeError):
            WorkUnit("x", WHEN, WHEN, END)

    def test_the_payload_is_reachable_under_both_names(self):
        """`payload` stays the shared name the pipeline reads."""
        unit = RasterWorkUnit("the-image", WHEN, WHEN, END)
        assert unit.payload is unit.image


class TestTupleCompatibility:
    """The four-tuple `_composite` used to yield still unpacks."""

    def test_unpacks_as_the_old_tuple(self):
        """Every existing caller reads `when, image, start, end`."""
        unit = RasterWorkUnit("img", WHEN, WHEN, END)
        when, payload, start, end = unit
        assert (when, payload, start, end) == (WHEN, "img", WHEN, END)

    def test_indexes_as_the_old_tuple(self):
        """Positional access keeps working too."""
        unit = RasterWorkUnit("img", WHEN, WHEN, END)
        assert unit[0] == WHEN and unit[1] == "img"

    def test_reports_the_tuple_length(self):
        """So `len()` on it is not surprising."""
        assert len(RasterWorkUnit("img", WHEN, WHEN, END)) == 4

    def test_as_tuple_is_explicit(self):
        """New code should prefer the named attributes, but this is honest."""
        assert RasterWorkUnit("img", WHEN, WHEN, END).as_tuple() == (
            WHEN,
            "img",
            WHEN,
            END,
        )

    def test_composite_yields_work_units(self, make_gee):
        """The pipeline produces typed units, not tuples."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        collection = gee._build_collection(var_info, ["elevation"], WHEN, END)
        units = list(gee._composite(collection, var_info, WHEN, END))
        assert units and all(isinstance(u, RasterWorkUnit) for u in units)


class TestArtifact:
    """What one bucket produced, written or in memory."""

    def test_a_written_path_is_the_value(self):
        """Preserves the synchronous sink's return."""
        artifact = Artifact(path=Path("out.tif"))
        assert artifact.value == Path("out.tif")

    def test_an_in_memory_object_takes_precedence(self):
        """When materialising, the object is what the caller asked for."""
        artifact = Artifact(path=Path("out.tif"), obj="the-dataset")
        assert artifact.value == "the-dataset"

    def test_a_destination_is_the_value_for_a_batch_sink(self):
        """Preserves the batch sink's return."""
        assert Artifact(destination="gs://b/p").value == "gs://b/p"

    def test_a_task_is_the_value_when_not_waiting(self):
        """Preserves the `wait_for_export=False` return."""
        assert Artifact(task="the-task").value == "the-task"

    def test_an_empty_artifact_has_no_value(self):
        """Rather than raising, so a caller can test for it."""
        assert Artifact().value is None

    def test_the_value_precedence_is_obj_path_destination_then_task(self):
        """The order is the pre-Epic-1 return contract, so it must not drift."""
        every = {
            "obj": "the-dataset",
            "path": Path("out.tif"),
            "destination": "gs://b/p",
            "task": "the-task",
        }
        assert Artifact(**every).value == "the-dataset"
        del every["obj"]
        assert Artifact(**every).value == Path("out.tif")
        del every["path"]
        assert Artifact(**every).value == "gs://b/p"
        del every["destination"]
        assert Artifact(**every).value == "the-task"

    def test_the_kind_is_carried_through(self):
        """So a caller tells a vector result from a raster one without the payload."""
        assert Artifact().kind == "raster"
        assert Artifact(kind="vector").kind == "vector"

    def test_summary_reads_as_one_line(self):
        """Artifacts appear in log lines, so they must stay readable."""
        text = str(Artifact(when=WHEN, path=Path("out.tif")))
        assert text.startswith("Artifact(") and "out.tif" in text


class TestDownloadResult:
    """It must stand in for the `list` `download()` used to return."""

    def _result(self):
        return DownloadResult(
            artifacts=(
                Artifact(path=Path("a.tif"), obj="ds-a"),
                Artifact(path=Path("b.tif"), obj="ds-b"),
            )
        )

    def test_iterates_the_payloads(self):
        """`for item in el.download()` keeps working."""
        assert list(self._result()) == ["ds-a", "ds-b"]

    def test_indexes_and_measures_like_a_list(self):
        """So does `result[0]` and `len(result)`."""
        result = self._result()
        assert result[0] == "ds-a"
        assert len(result) == 2

    def test_compares_equal_to_the_plain_list(self):
        """Which is what every existing test and notebook asserts against."""
        assert self._result() == ["ds-a", "ds-b"]

    def test_compares_equal_to_a_peer(self):
        """Value semantics between two results."""
        assert self._result() == self._result()

    def test_is_not_equal_to_an_unrelated_type(self):
        """Comparison against something else is simply false, not an error."""
        assert self._result() != 3

    def test_exposes_paths_and_objects_separately(self):
        """A caller can ask for either half."""
        result = self._result()
        assert result.paths == [Path("a.tif"), Path("b.tif")]
        assert result.objects == ["ds-a", "ds-b"]

    def test_paths_skips_a_batch_artifact(self):
        """A queued export wrote nothing locally."""
        result = DownloadResult(artifacts=(Artifact(destination="gs://b/p"),))
        assert result.paths == []
        assert result.values == ["gs://b/p"]

    def test_objects_skips_an_artifact_that_materialised_nothing(self):
        """Only the buckets that were materialised contribute an object."""
        result = DownloadResult(
            artifacts=(
                Artifact(path=Path("a.tif")),
                Artifact(path=Path("b.tif"), obj="ds-b"),
            )
        )
        assert result.objects == ["ds-b"]
        assert len(result.paths) == 2

    def test_an_empty_result_stands_in_for_an_empty_list(self):
        """A download that produced nothing must not need a special case."""
        empty = DownloadResult()
        assert list(empty) == []
        assert len(empty) == 0
        assert empty == []
        assert empty.paths == [] and empty.objects == []

    def test_an_empty_result_is_hashable(self):
        """`__eq__` is overridden, so `__hash__` has to be supplied with it."""
        assert hash(DownloadResult()) == hash(DownloadResult())

    def test_an_out_of_range_index_raises(self):
        """Indexing behaves like the list it replaced, including on overflow."""
        with pytest.raises(IndexError):
            self._result()[5]

    def test_is_not_equal_to_a_tuple_of_the_same_payloads(self):
        """Only a `list` is accepted as the pre-Epic-1 return, not any sequence."""
        assert self._result() != ("ds-a", "ds-b")


class TestMaterializeThroughDownload:
    """`materialize=` returns the in-memory objects as well as the paths."""

    def test_off_by_default_the_return_is_unchanged(self, make_gee):
        """Today's contract: a list of written paths."""
        result = make_gee(engine="ee").download(progress_bar=False)
        assert isinstance(result, list)
        assert all(isinstance(entry, Path) for entry in result)

    def test_materialize_returns_a_download_result(self, make_gee):
        """Opt in and the objects come back alongside the paths."""
        result = make_gee(engine="ee").download(progress_bar=False, materialize=True)
        assert isinstance(result, DownloadResult)
        assert result.paths and result.objects

    def test_the_constructor_flag_has_the_same_effect(self, make_gee):
        """`GEE(materialize=True)` is equivalent to passing it per call."""
        result = make_gee(engine="ee", materialize=True).download(progress_bar=False)
        assert isinstance(result, DownloadResult)

    def test_the_call_argument_overrides_the_constructor(self, make_gee):
        """So one call can opt out of an instance-wide default."""
        gee = make_gee(engine="ee", materialize=True)
        assert isinstance(gee.download(progress_bar=False, materialize=False), list)

    def test_it_still_compares_equal_to_the_path_list(self, make_gee):
        """The result stands in for the list even when materialising."""
        gee = make_gee(engine="ee")
        paths = gee.download(progress_bar=False)
        materialised = gee.download(progress_bar=False, materialize=True)
        assert materialised.paths == paths

    def test_a_batch_sink_cannot_materialise(self, make_gee):
        """A queued export leaves nothing on disk to read back."""
        gee = make_gee(export_via="drive", drive_folder="f")
        with pytest.raises(ValueError, match="queues the work remotely"):
            gee.download(progress_bar=False, materialize=True)

    def test_the_refusal_names_the_alternative(self, make_gee):
        """It should say what to do instead, not just refuse."""
        gee = make_gee(export_via="gcs", gcs_bucket="b")
        with pytest.raises(ValueError, match="export_via='url'"):
            gee.download(progress_bar=False, materialize=True)

    def test_output_kind_is_unchanged_by_materialising(self, make_gee):
        """The *kind* is still raster; only the container differs."""
        gee = make_gee(engine="ee", materialize=True)
        assert gee.OUTPUT_KIND == "raster"
