"""Tests for the TableSink feature-collection batch export."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from earthlens.gee import jobs as jobs_module
from earthlens.gee import table_sink as table_sink_module
from earthlens.gee.jobs import get_task_status
from earthlens.gee.table_sink import TableSink
from earthlens.gee.work import FeatureWorkUnit

_WHEN = dt.datetime(2024, 1, 1)
_SENTINEL = object()


class _FakeTask:
    """Records start() and returns a stub status()."""

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def status(self) -> dict[str, str]:
        return {"state": "READY", "id": "task-1"}


class _FakeTableExport:
    """Stands in for `ee.batch.Export.table`, recording the factory + kwargs."""

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

    def toAsset(self, **kwargs: object) -> _FakeTask:  # noqa: N802
        return self._record("toAsset", **kwargs)

    def toBigQuery(self, **kwargs: object) -> _FakeTask:  # noqa: N802
        return self._record("toBigQuery", **kwargs)

    def toFeatureView(self, **kwargs: object) -> _FakeTask:  # noqa: N802
        return self._record("toFeatureView", **kwargs)


@pytest.fixture
def fake_export(monkeypatch):
    """Patch the EE table-export factory and the TaskInfo/wait seams."""
    export = _FakeTableExport()
    waited: list[object] = []
    monkeypatch.setattr("ee.batch.Export.table", export, raising=False)
    monkeypatch.setattr(table_sink_module, "_op_to_taskinfo", lambda status: _SENTINEL)
    monkeypatch.setattr(
        table_sink_module, "wait_for_task", lambda task, **_: waited.append(task)
    )
    export.waited = waited
    return export


class TestTableSinkConfig:
    """The frozen export configuration."""

    def test_defaults(self):
        """A bare sink targets Drive, CSV, non-blocking."""
        sink = TableSink()
        assert (sink.destination, sink.file_format, sink.wait_for_export) == (
            "drive",
            "CSV",
            False,
        )

    def test_it_is_frozen(self):
        """The configuration cannot drift after construction."""
        sink = TableSink(destination="gcs", bucket="b")
        with pytest.raises(Exception, match="frozen"):
            sink.bucket = "other"


class TestTableSinkValidation:
    """Option/destination consistency, checked at construction."""

    def test_overwrite_and_append_are_mutually_exclusive(self):
        """Earth Engine refuses both flags, so the sink refuses them first."""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            TableSink(
                destination="bigquery", table="p.d.t", overwrite=True, append=True
            )

    def test_overwrite_is_bigquery_only(self):
        """The overwrite flag has no meaning on a file destination."""
        with pytest.raises(ValidationError, match="BigQuery"):
            TableSink(destination="drive", folder="f", overwrite=True)

    def test_append_is_bigquery_only(self):
        """The append flag has no meaning on a file destination."""
        with pytest.raises(ValidationError, match="BigQuery"):
            TableSink(destination="gcs", bucket="b", append=True)

    def test_ingestion_time_parameters_is_featureview_only(self):
        """The serving options belong to FeatureView alone."""
        with pytest.raises(ValidationError, match="FeatureView"):
            TableSink(
                destination="bigquery",
                table="p.d.t",
                ingestion_time_parameters={"maxFeaturesPerTile": 1000},
            )

    @pytest.mark.parametrize(
        ("destination", "missing"),
        [
            ("gcs", "bucket"),
            ("asset", "asset_id"),
            ("bigquery", "table"),
            ("featureview", "asset_id"),
        ],
    )
    def test_each_destination_requires_its_address(self, destination, missing):
        """A sink without the address its destination writes to is refused."""
        with pytest.raises(ValidationError, match=missing):
            TableSink(destination=destination)

    def test_an_unknown_file_format_is_refused(self):
        """The format is a fixed vocabulary, not a free string EE may reject."""
        with pytest.raises(ValidationError, match="file_format"):
            TableSink(destination="drive", folder="f", file_format="Parquet")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"destination": "asset", "asset_id": "a/b"},
            {"destination": "bigquery", "table": "p.d.t"},
            {"destination": "featureview", "asset_id": "a/b"},
        ],
        ids=["asset", "bigquery", "featureview"],
    )
    def test_a_file_format_is_refused_where_it_is_ignored(self, kwargs):
        """A format the destination never passes on is a mistake, not a no-op."""
        with pytest.raises(ValidationError, match="file_format"):
            TableSink(file_format="GeoJSON", **kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"destination": "drive", "folder": "f"},
            {"destination": "gcs", "bucket": "b"},
        ],
        ids=["drive", "gcs"],
    )
    def test_the_file_destinations_take_a_format(self, kwargs):
        """The two destinations that forward `fileFormat` still accept it."""
        assert TableSink(file_format="GeoJSON", **kwargs).file_format == "GeoJSON"

    @pytest.mark.parametrize("table", ["nodots", "p.d", "p.d.", ".d.t"])
    def test_a_malformed_bigquery_table_is_refused(self, table):
        """The sink mirrors EE's own `project.dataset.table` check."""
        with pytest.raises(ValidationError, match="project.dataset.table"):
            TableSink(destination="bigquery", table=table)

    def test_the_bigquery_shape_matches_the_sdk(self):
        """The accepted/refused split is the SDK's regex, not a guess."""
        import re

        import ee.batch  # noqa: F401 - the SDK whose rule is mirrored

        sdk = re.compile(r".+\..+\..+")
        for table in ("p.d.t", "proj-1.data_set.tbl"):
            assert sdk.fullmatch(table) is not None
            assert TableSink(destination="bigquery", table=table).table == table
        for table in ("nodots", "p.d"):
            assert sdk.fullmatch(table) is None
            with pytest.raises(ValidationError):
                TableSink(destination="bigquery", table=table)

    def test_drive_needs_no_folder(self):
        """Earth Engine exports to the Drive root when no folder is given."""
        assert TableSink(destination="drive").folder is None


class TestTableSinkWrite:
    """Each destination dispatches to its Export.table factory."""

    def test_drive(self, fake_export):
        """Drive passes folder + file format and returns a TaskInfo."""
        result = TableSink(destination="drive", folder="exports").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toDrive"
        assert kwargs["folder"] == "exports"
        assert kwargs["fileFormat"] == "CSV"
        assert fake_export.task.started is True
        assert result is _SENTINEL

    def test_gcs(self, fake_export):
        """GCS passes the bucket."""
        TableSink(destination="gcs", bucket="my-bucket").write("fc", description="d")
        name, kwargs = fake_export.calls[0]
        assert name == "toCloudStorage"
        assert kwargs["bucket"] == "my-bucket"

    def test_asset(self, fake_export):
        """Asset passes the asset id."""
        TableSink(destination="asset", asset_id="users/me/pts").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toAsset"
        assert kwargs["assetId"] == "users/me/pts"

    def test_bigquery(self, fake_export):
        """BigQuery passes the table and the write-disposition defaults."""
        TableSink(destination="bigquery", table="proj.ds.tbl").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toBigQuery"
        assert kwargs["table"] == "proj.ds.tbl"
        assert (kwargs["overwrite"], kwargs["append"]) == (False, False)

    def test_bigquery_passes_overwrite(self, fake_export):
        """The overwrite flag reaches ee."""
        TableSink(destination="bigquery", table="proj.ds.tbl", overwrite=True).write(
            "fc", description="d"
        )
        _, kwargs = fake_export.calls[0]
        assert (kwargs["overwrite"], kwargs["append"]) == (True, False)

    def test_bigquery_passes_append(self, fake_export):
        """The append flag reaches ee."""
        TableSink(destination="bigquery", table="proj.ds.tbl", append=True).write(
            "fc", description="d"
        )
        _, kwargs = fake_export.calls[0]
        assert (kwargs["overwrite"], kwargs["append"]) == (False, True)

    def test_featureview(self, fake_export):
        """FeatureView passes the asset id, with no serving options by default."""
        TableSink(destination="featureview", asset_id="users/me/fv").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toFeatureView"
        assert kwargs["assetId"] == "users/me/fv"
        assert kwargs["ingestionTimeParameters"] is None

    def test_featureview_passes_ingestion_time_parameters(self, fake_export):
        """The serving options travel inside the single ee dict."""
        parameters = {
            "maxFeaturesPerTile": 1500,
            "thinningStrategy": "HIGHER_DENSITY",
        }
        TableSink(
            destination="featureview",
            asset_id="users/me/fv",
            ingestion_time_parameters=parameters,
        ).write("fc", description="d")
        _, kwargs = fake_export.calls[0]
        assert kwargs["ingestionTimeParameters"] == parameters

    def test_wait_for_export_blocks(self, fake_export):
        """With wait_for_export the task is waited on before the TaskInfo."""
        TableSink(destination="drive", folder="f", wait_for_export=True).write(
            "fc", description="d"
        )
        assert fake_export.waited == [fake_export.task]

    def test_no_wait_does_not_block(self, fake_export):
        """Without wait_for_export the task is not waited on."""
        TableSink(destination="drive", folder="f").write("fc", description="d")
        assert fake_export.waited == []

    def test_export_from_work_unit(self, fake_export):
        """export pulls the collection off a FeatureWorkUnit and labels it."""
        unit = FeatureWorkUnit("fc", _WHEN, _WHEN, _WHEN)
        result = TableSink(destination="drive", folder="f").export(
            unit, description="my-desc"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toDrive"
        assert kwargs["description"] == "my-desc"
        assert result is _SENTINEL

    def test_export_default_description_is_the_unit_repr(self, fake_export):
        """Without an explicit description, the unit's repr is used."""
        unit = FeatureWorkUnit("fc", _WHEN, _WHEN, _WHEN)
        TableSink(destination="drive", folder="f").export(unit)
        _, kwargs = fake_export.calls[0]
        assert kwargs["description"] == repr(unit)


class _FakeFeaturesTask:
    """A task whose status carries the table-export wire type."""

    def start(self) -> None:
        """Accept the submission."""

    def status(self) -> dict[str, object]:
        return {
            "state": "READY",
            "id": "task-1",
            "task_type": "EXPORT_FEATURES",
            "description": "d",
            "creation_timestamp_ms": 1_704_067_200_000,
            "update_timestamp_ms": 1_704_067_200_000,
        }


class TestTaskTracking:
    """A queued table export rides the unchanged jobs machinery."""

    def test_the_table_wire_type_is_export_features(self):
        """Every table target — BigQuery and FeatureView included — is EXPORT_FEATURES."""
        import ee.batch

        assert ee.batch.Task.Type.EXPORT_TABLE.value == "EXPORT_FEATURES"

    def test_bigquery_write_returns_a_taskinfo_with_the_table_wire_type(
        self, monkeypatch
    ):
        """The submitted status normalises to a TaskInfo typed EXPORT_FEATURES."""
        export = _FakeTableExport()
        export.task = _FakeFeaturesTask()
        monkeypatch.setattr("ee.batch.Export.table", export, raising=False)
        info = TableSink(destination="bigquery", table="proj.ds.tbl").write(
            "fc", description="d"
        )
        assert (info.id, info.state, info.task_type) == (
            "task-1",
            "READY",
            "EXPORT_FEATURES",
        )

    def test_the_queued_task_round_trips_through_get_task_status(self, monkeypatch):
        """The TaskInfo id resolves through jobs.get_task_status."""
        operation = {
            "name": "projects/p/operations/task-1",
            "metadata": {
                "state": "RUNNING",
                "type": "EXPORT_FEATURES",
                "createTime": "2024-01-01T00:00:00Z",
                "updateTime": "2024-01-01T00:01:00Z",
                "description": "d",
            },
            "done": False,
        }
        monkeypatch.setattr(jobs_module.ee.data, "getOperation", lambda name: operation)
        info = get_task_status("task-1", project="p")
        assert (info.id, info.state, info.task_type) == (
            "task-1",
            "RUNNING",
            "EXPORT_FEATURES",
        )
