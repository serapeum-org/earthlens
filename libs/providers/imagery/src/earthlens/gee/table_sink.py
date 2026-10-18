"""`TableSink`: export an `ee.FeatureCollection` as a batch task.

The image sinks in `earthlens.gee.sinks` write a composited `ee.Image`; their
`write` is image-shaped (it takes `var_info`, `bands`, and a `region` to clip),
so a vector / table export does not fit that contract. `TableSink` is the
table-shaped counterpart: it dispatches a `FeatureCollection` to the matching
`ee.batch.Export.table.to*` factory and tracks the queued task through the same
`earthlens.gee.jobs` machinery (`TaskInfo`, `wait_for_task_id`,
`resolve_destination`) the image exports use, so a vector export is followed up
exactly like an image one.

The `drive` / `gcs` / `asset` trio is the core; `bigquery` / `featureview` are
the richer destinations a later epic may extend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from earthlens.gee._helpers import wait_for_task
from earthlens.gee.jobs import TaskInfo, _op_to_taskinfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee

    from earthlens.gee.work import FeatureWorkUnit

__all__ = ["TableSink"]


class TableSink(BaseModel):
    """Queue an `ee.FeatureCollection` export and track it as a `TaskInfo`.

    A frozen value object holding the destination and its address. `export`
    (from a `FeatureWorkUnit`) or `write` (from a raw collection) dispatches to
    `ee.batch.Export.table.to{Drive,CloudStorage,Asset,BigQuery,FeatureView}`,
    starts the task, and returns the `TaskInfo` the rest of
    `earthlens.gee.jobs` tracks by id. With `wait_for_export=True` it blocks
    until the task reaches a terminal state first.

    Args:
        destination: Which `Export.table.to*` factory to use. Defaults to
            `"drive"`.
        folder: The Drive folder (`drive`).
        bucket: The Cloud Storage bucket (`gcs`).
        asset_id: The asset path (`asset` / `featureview`).
        table: The BigQuery table (`bigquery`).
        file_format: The export file format for the file destinations.
            Defaults to `"CSV"`.
        wait_for_export: Block until the task finishes before returning.
            Defaults to `False`.

    Examples:
        - A sink is a frozen value object, constructed without Earth Engine:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> sink = TableSink(destination="gcs", bucket="my-bucket", file_format="GeoJSON")
            >>> (sink.destination, sink.bucket, sink.file_format, sink.wait_for_export)
            ('gcs', 'my-bucket', 'GeoJSON', False)

            ```
        - Its configuration cannot drift after construction:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> sink = TableSink(destination="drive", folder="exports")
            >>> sink.folder = "elsewhere"
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: Literal["drive", "gcs", "asset", "bigquery", "featureview"] = "drive"
    folder: str | None = None
    bucket: str | None = None
    asset_id: str | None = None
    table: str | None = None
    file_format: str = "CSV"
    wait_for_export: bool = False

    def export(
        self, work_unit: FeatureWorkUnit, *, description: str | None = None
    ) -> TaskInfo:
        """Export a `FeatureWorkUnit`'s features as a tracked table task.

        Args:
            work_unit: The `FeatureWorkUnit` carrying the `ee.FeatureCollection`.
            description: The task description; defaults to the unit's `repr`.

        Returns:
            The `TaskInfo` for the queued (or completed) export.
        """
        label = description if description is not None else repr(work_unit)
        return self.write(work_unit.features, description=label)

    def write(self, features: ee.FeatureCollection, *, description: str) -> TaskInfo:
        """Queue `features` to this destination and return its `TaskInfo`.

        Args:
            features: The `ee.FeatureCollection` to export.
            description: The Earth Engine task description.

        Returns:
            The `TaskInfo` captured at submission, or after the task reaches a
            terminal state when `wait_for_export` is `True`.
        """
        import ee

        factory = ee.batch.Export.table
        if self.destination == "drive":
            task = factory.toDrive(
                collection=features,
                description=description,
                folder=self.folder,
                fileFormat=self.file_format,
            )
        elif self.destination == "gcs":
            task = factory.toCloudStorage(
                collection=features,
                description=description,
                bucket=self.bucket,
                fileFormat=self.file_format,
            )
        elif self.destination == "asset":
            task = factory.toAsset(
                collection=features, description=description, assetId=self.asset_id
            )
        elif self.destination == "bigquery":
            task = factory.toBigQuery(
                collection=features, description=description, table=self.table
            )
        else:  # "featureview"
            task = factory.toFeatureView(
                collection=features, description=description, assetId=self.asset_id
            )
        task.start()
        if self.wait_for_export:
            wait_for_task(task, progress_bar=False)
        return _op_to_taskinfo(task.status())
