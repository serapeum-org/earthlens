"""Tests for the TableSink feature-collection batch export."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.gee import table_sink as table_sink_module
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


class TestTableSinkWrite:
    """Each destination dispatches to its Export.table factory."""

    def test_drive(self, fake_export):
        """Drive passes folder + file format and returns a TaskInfo."""
        result = TableSink(destination="drive", folder="exports").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toDrive"
        assert kwargs["folder"] == "exports" and kwargs["fileFormat"] == "CSV"
        assert fake_export.task.started is True
        assert result is _SENTINEL

    def test_gcs(self, fake_export):
        """GCS passes the bucket."""
        TableSink(destination="gcs", bucket="my-bucket").write("fc", description="d")
        name, kwargs = fake_export.calls[0]
        assert name == "toCloudStorage" and kwargs["bucket"] == "my-bucket"

    def test_asset(self, fake_export):
        """Asset passes the asset id."""
        TableSink(destination="asset", asset_id="users/me/pts").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toAsset" and kwargs["assetId"] == "users/me/pts"

    def test_bigquery(self, fake_export):
        """BigQuery passes the table."""
        TableSink(destination="bigquery", table="proj.ds.tbl").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toBigQuery" and kwargs["table"] == "proj.ds.tbl"

    def test_featureview(self, fake_export):
        """FeatureView passes the asset id."""
        TableSink(destination="featureview", asset_id="users/me/fv").write(
            "fc", description="d"
        )
        name, kwargs = fake_export.calls[0]
        assert name == "toFeatureView" and kwargs["assetId"] == "users/me/fv"

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
        assert name == "toDrive" and kwargs["description"] == "my-desc"
        assert result is _SENTINEL

    def test_export_default_description_is_the_unit_repr(self, fake_export):
        """Without an explicit description, the unit's repr is used."""
        unit = FeatureWorkUnit("fc", _WHEN, _WHEN, _WHEN)
        TableSink(destination="drive", folder="f").export(unit)
        _, kwargs = fake_export.calls[0]
        assert kwargs["description"] == repr(unit)
