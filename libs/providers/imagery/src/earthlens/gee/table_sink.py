"""`TableSink`: export an `ee.FeatureCollection` as a batch task.

The image sinks in `earthlens.gee.sinks` write a composited `ee.Image`; their
`write` is image-shaped (it takes `var_info`, `bands`, and a `region` to clip),
so a vector / table export does not fit that contract. `TableSink` is the
table-shaped counterpart: it dispatches a `FeatureCollection` to the matching
`ee.batch.Export.table.to*` factory and tracks the queued task through the same
`earthlens.gee.jobs` machinery (`TaskInfo`, `wait_for_task_id`,
`resolve_destination`) the image exports use, so a vector export is followed up
exactly like an image one.

The `drive` / `gcs` / `asset` trio is the core; `bigquery` / `featureview`
carry their server-side options too — BigQuery's mutually exclusive
`overwrite` / `append` write disposition, and FeatureView's
`ingestion_time_parameters` dict (`maxFeaturesPerTile`, the thinning strategy
and friends all travel *inside* that one dict, as
`ee.batch.Export.table.toFeatureView` takes them). Every destination's
required address is validated at construction (see `_REQUIRED_ADDRESS`), so a
misconfigured sink fails at the call that built it rather than as a late
server error. Whatever the destination, the queued task's wire type — what
`TaskInfo.task_type` carries — is `EXPORT_FEATURES`: BigQuery and FeatureView
create the same `ee.batch.Task.Type.EXPORT_TABLE` tasks the file targets do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from earthlens.gee._helpers import wait_for_task
from earthlens.gee.jobs import TaskInfo, _op_to_taskinfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee

    from earthlens.gee.work import FeatureWorkUnit

__all__ = ["TableSink"]

#: The address field each destination must carry, enforced at construction.
#: `drive` is deliberately absent: Earth Engine exports to the Drive root
#: when no folder is given.
_REQUIRED_ADDRESS: dict[str, str] = {
    "gcs": "bucket",
    "asset": "asset_id",
    "bigquery": "table",
    "featureview": "asset_id",
}


class TableSink(BaseModel):
    """Queue an `ee.FeatureCollection` export and track it as a `TaskInfo`.

    A frozen value object holding the destination and its address. `export`
    (from a `FeatureWorkUnit`) or `write` (from a raw collection) dispatches to
    `ee.batch.Export.table.to{Drive,CloudStorage,Asset,BigQuery,FeatureView}`,
    starts the task, and returns the `TaskInfo` the rest of
    `earthlens.gee.jobs` tracks by id — its `task_type` is the
    `EXPORT_FEATURES` wire value for every destination, BigQuery and
    FeatureView included. With `wait_for_export=True` it blocks until the
    task reaches a terminal state first.

    Construction refuses an inconsistent sink: each destination must carry
    its address (`bucket` for `gcs`, `asset_id` for `asset` / `featureview`,
    `table` for `bigquery` — `drive` without a `folder` lands in the Drive
    root, Earth Engine's own default), `overwrite` / `append` are mutually
    exclusive and BigQuery-only, and `ingestion_time_parameters` is
    FeatureView-only.

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
        - A destination without its required address is refused up front:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> TableSink(destination="bigquery")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - So is an impossible BigQuery write disposition:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> TableSink(
            ...     destination="bigquery", table="p.d.t", overwrite=True, append=True
            ... )
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: Literal["drive", "gcs", "asset", "bigquery", "featureview"] = (
        "drive"  # Export.table.to* factory
    )
    folder: str | None = None  # Drive folder (drive); None is the Drive root
    bucket: str | None = None  # Cloud Storage bucket (gcs)
    asset_id: str | None = None  # asset path (asset / featureview)
    table: str | None = None  # BigQuery "project.dataset.table" (bigquery)
    overwrite: bool = False  # BigQuery: replace an existing table
    append: bool = False  # BigQuery: add to an existing table
    ingestion_time_parameters: dict[str, Any] | None = (
        None  # FeatureView serving options, passed as one ee dict
    )
    file_format: str = "CSV"  # export file format for the file destinations
    wait_for_export: bool = False  # block until the task finishes

    @model_validator(mode="after")
    def _consistent(self) -> TableSink:
        """Refuse option/destination mismatches and a missing address.

        Returns:
            The config unchanged once it is consistent.

        Raises:
            ValueError: If `overwrite` and `append` are both set, either is
                set for a non-BigQuery destination, `ingestion_time_parameters`
                is set for a non-FeatureView destination, or the destination's
                required address field is `None`.
        """
        if self.overwrite and self.append:
            raise ValueError("overwrite and append are mutually exclusive")
        if (self.overwrite or self.append) and self.destination != "bigquery":
            raise ValueError(
                "overwrite/append are BigQuery write dispositions; the "
                f"destination is {self.destination!r}"
            )
        if (
            self.ingestion_time_parameters is not None
            and self.destination != "featureview"
        ):
            raise ValueError(
                "ingestion_time_parameters is a FeatureView serving option; "
                f"the destination is {self.destination!r}"
            )
        address = _REQUIRED_ADDRESS.get(self.destination)
        if address is not None and getattr(self, address) is None:
            raise ValueError(f"destination {self.destination!r} requires {address}=")
        return self

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
                collection=features,
                description=description,
                table=self.table,
                overwrite=self.overwrite,
                append=self.append,
            )
        else:  # "featureview"
            task = factory.toFeatureView(
                collection=features,
                description=description,
                assetId=self.asset_id,
                ingestionTimeParameters=self.ingestion_time_parameters,
            )
        task.start()
        if self.wait_for_export:
            wait_for_task(task, progress_bar=False)
        return _op_to_taskinfo(task.status())
