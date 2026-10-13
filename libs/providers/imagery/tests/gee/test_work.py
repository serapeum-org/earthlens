"""Tests for the work units and the download-result contract."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from earthlens.gee.work import (
    Artifact,
    DownloadResult,
    FeatureWorkUnit,
    RasterWorkUnit,
    WorkUnit,
)

from .test_backend import fake_ee, fake_reader, make_gee  # noqa: F401 - fixtures


class _Handle:
    """A stand-in for a pyramids dataset that records being closed."""

    def __init__(self):
        self.closed = False

    def close(self):
        """Record the close."""
        self.closed = True


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
        assert unit[0] == WHEN
        assert unit[1] == "img"

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
        assert units
        assert all(isinstance(u, RasterWorkUnit) for u in units)


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
        assert text.startswith("Artifact(")
        assert "out.tif" in text


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
        first = self._result()
        second = self._result()
        assert first == second

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
        assert empty.paths == []
        assert empty.objects == []

    def test_an_empty_result_is_hashable(self):
        """`__eq__` is overridden, so `__hash__` has to be supplied with it."""
        default = DownloadResult()
        explicit = DownloadResult(artifacts=())
        assert hash(default) == hash(explicit)

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
        assert result.paths
        assert result.objects

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


class TestMaterialiseKinds:
    """`_materialise` wraps whatever the sink returned, not only paths."""

    def test_a_destination_string_becomes_a_destination_artifact(
        self, make_gee, fake_reader
    ):
        """A batch sink returns a URI, which is the artifact's value."""
        result = make_gee()._materialise(["drive://folder/prefix"])
        assert result.values == ["drive://folder/prefix"]

    def test_a_task_becomes_a_task_artifact(self, make_gee, fake_reader):
        """`wait_for_export=False` returns a handle rather than a destination."""
        import datetime as _dt

        from earthlens.gee.jobs import TaskInfo

        now = _dt.datetime(2026, 1, 1)
        task = TaskInfo(
            id="T1",
            operation_name="projects/p/operations/T1",
            description="d",
            state="READY",
            task_type="EXPORT_IMAGE",
            create_time=now,
            update_time=now,
        )
        result = make_gee()._materialise([task])
        assert result.values == [task]

    def test_a_batch_sink_is_refused_at_construction(self, make_gee, fake_reader):
        """Before any export runs, so a polled run cannot lose its destinations."""
        with pytest.raises(ValueError, match="materialize=True needs pixels"):
            make_gee(export_via="drive", drive_folder="out", materialize=True)


class TestClosingAMaterialisedResult:
    """Materialising hands back live GDAL handles, so they must be releasable."""

    def _result(self, *handles):
        return DownloadResult(
            artifacts=tuple(Artifact(path=Path("a.tif"), obj=h) for h in handles)
        )

    def test_close_closes_every_held_object(self):
        """One open handle per bucket, all released together."""
        first, second = _Handle(), _Handle()
        self._result(first, second).close()
        assert first.closed
        assert second.closed

    def test_close_is_idempotent(self):
        """Calling it twice must not raise."""
        handle = _Handle()
        result = self._result(handle)
        result.close()
        result.close()
        assert handle.closed

    def test_it_works_as_a_context_manager(self):
        """The shape a caller should reach for."""
        handle = _Handle()
        with self._result(handle) as result:
            assert result.objects == [handle]
            assert not handle.closed
        assert handle.closed

    def test_a_value_that_is_not_a_dataset_is_skipped(self):
        """Closing must never raise on an object with no `close`."""
        DownloadResult(artifacts=(Artifact(obj="not a dataset"),)).close()

    def test_an_unmaterialised_result_closes_cleanly(self):
        """Nothing was opened, so there is nothing to release."""
        DownloadResult(artifacts=(Artifact(path=Path("a.tif")),)).close()


class TestFrozenValueObjects:
    """Round 1 made these frozen and strict; that must stay asserted."""

    def test_an_artifact_is_frozen(self):
        """It is a value object, like every other Epic 1 leaf."""
        artifact = Artifact(path=Path("a.tif"))
        with pytest.raises(ValidationError):
            artifact.kind = "vector"

    def test_an_artifact_rejects_an_unknown_field(self):
        """A mistyped keyword must not be silently dropped."""
        with pytest.raises(ValidationError):
            Artifact(bogus=1)

    def test_a_download_result_is_frozen(self):
        """Same contract on the container."""
        result = DownloadResult(artifacts=())
        with pytest.raises(ValidationError):
            result.artifacts = ()

    def test_a_download_result_rejects_an_unknown_field(self):
        """Same contract on the container."""
        with pytest.raises(ValidationError):
            DownloadResult(artifacts=(), bogus=1)

    def test_a_populated_result_is_hashable(self):
        """The case the inherited permissive config used to break."""
        assert isinstance(hash(DownloadResult(artifacts=(Artifact(obj="x"),))), int)


class TestEqualityIsTransitive:
    """Comparing equal to a list and to a peer must agree."""

    def test_two_results_with_the_same_payloads_are_equal(self):
        """Even when one carries the object and the other only the path."""
        from pathlib import Path as _P

        written = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        same = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        assert written == same

    def test_equality_is_transitive_through_the_list(self):
        """The case the artifact-wise comparison broke."""
        from pathlib import Path as _P

        first = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        second = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        payloads = [_P("a.tif")]
        assert first == payloads
        assert second == payloads
        assert first == second

    def test_differing_payloads_are_not_equal(self):
        """Transitivity must not be bought with blanket equality."""
        from pathlib import Path as _P

        assert DownloadResult(artifacts=(Artifact(path=_P("a.tif")),)) != (
            DownloadResult(artifacts=(Artifact(path=_P("b.tif")),))
        )

    def test_equal_results_hash_alike(self):
        """What `__hash__` has to guarantee for `__eq__`."""
        from pathlib import Path as _P

        first = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        second = DownloadResult(artifacts=(Artifact(path=_P("a.tif")),))
        assert first == second
        assert hash(first) == hash(second)

    def test_a_materialised_result_is_hashable(self):
        """A GDAL-backed payload need not be hashable, but the result must be."""
        assert isinstance(
            hash(DownloadResult(artifacts=(Artifact(obj=_Handle()),))), int
        )


class TestEngineConfigIsLive:
    """It is derived, so it cannot disagree with the public attributes."""

    def test_it_tracks_a_reassigned_attribute(self, make_gee):
        """A snapshot taken in `__init__` would have gone stale here."""
        gee = make_gee(crs="EPSG:4326")
        gee.crs = "EPSG:3857"
        assert gee._engine_config.crs == "EPSG:3857"

    def test_the_eligibility_check_reads_it(self, make_gee, fake_reader):
        """One definition of "is anything shaping this server-side?"."""
        gee = make_gee()
        var_info = gee.catalog.get_dataset("USGS/SRTMGL1_003")
        assert gee._eedai_eligible(var_info) is True
        gee.cloud_mask = lambda image: image
        assert gee._engine_config.needs_server_side_compute is True
        assert gee._eedai_eligible(var_info) is False
