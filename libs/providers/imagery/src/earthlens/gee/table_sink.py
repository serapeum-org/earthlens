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
carry their server-side options too — `overwrite` (replace what is already
there) and `append` (add to it), and FeatureView's
`ingestion_time_parameters` dict (`maxFeaturesPerTile`, the thinning strategy
and friends all travel *inside* that one dict, as
`ee.batch.Export.table.toFeatureView` takes them). Every destination's
required address is validated at construction (see `_REQUIRED_ADDRESS`), so a
misconfigured sink fails at the call that built it rather than as a late
server error — and so is every option that is scoped to a subset of the
destinations. Each scope is the set of factories whose signature declares the
parameter, not a framing: `overwrite` is for `bigquery` **and** `asset`
(`toAsset` declares and forwards it, so a table asset can be re-exported in
place), `append` is BigQuery's alone, `ingestion_time_parameters` is
FeatureView's alone, and `file_format` belongs to the two destinations that
write a file (`_FILE_DESTINATIONS`), the rest ignoring it. `overwrite` and
`append` stay mutually exclusive, which is BigQuery's own rule. The BigQuery
`table` is checked for *shape* as well as presence, mirroring the SDK's own
`project.dataset.table` rule (`_BIGQUERY_TABLE_RE`), so that mistake lands
here rather than as an `EEException` from `write()`. Whatever the destination,
the queued task's wire type — what
`TaskInfo.task_type` carries — is `EXPORT_FEATURES`: BigQuery and FeatureView
create the same `ee.batch.Task.Type.EXPORT_TABLE` tasks the file targets do.
"""

from __future__ import annotations

import re
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

#: The destinations that forward `fileFormat` to their `Export.table.to*`
#: factory. The others write a table, not a file, and would ignore it — so a
#: `file_format` set for one of them is refused rather than dropped.
_FILE_DESTINATIONS: frozenset[str] = frozenset({"drive", "gcs"})

#: The destinations whose factory takes `overwrite`. Not BigQuery alone:
#: `ee.batch.Export.table.toAsset(..., overwrite=False)` is a declared,
#: forwarded parameter too ("If an existing asset can be overwritten by this
#: export"), which is what makes re-exporting a table asset in place
#: expressible. Scoped from the installed SDK's signatures rather than from
#: the "write disposition" framing, which is BigQuery's alone.
_OVERWRITE_DESTINATIONS: frozenset[str] = frozenset({"bigquery", "asset"})

#: The destinations whose factory takes `append`. Here BigQuery really is
#: alone: no other `Export.table.to*` has the parameter.
_APPEND_DESTINATIONS: frozenset[str] = frozenset({"bigquery"})

#: Earth Engine's own shape check for a BigQuery destination, mirrored so the
#: mistake lands at construction instead of as an `EEException` from
#: `ee.batch.Export.table.toBigQuery` at `write()` time. Verified against
#: earthengine-api's `re.fullmatch(r'.+\..+\..+', table)`.
_BIGQUERY_TABLE_RE = re.compile(r".+\..+\..+")


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
    `table` for `bigquery` in Earth Engine's `project.dataset.table` shape —
    `drive` without a `folder` lands in the Drive root, Earth Engine's own
    default), `overwrite` / `append` are mutually exclusive, `overwrite` is
    for `bigquery` / `asset` and `append` for `bigquery`,
    `ingestion_time_parameters` is FeatureView-only, and `file_format` is for
    the `drive` / `gcs` destinations that pass it on.

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
        - A BigQuery `table` must be fully qualified, not just
          `dataset.table`:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> TableSink(destination="bigquery", table="dataset.table")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
        - And a `file_format` is refused by the destinations that write a
          table rather than a file, instead of being silently dropped:
            ```python
            >>> from earthlens.gee.table_sink import TableSink
            >>> TableSink(
            ...     destination="asset",
            ...     asset_id="projects/p/assets/t",
            ...     file_format="GeoJSON",
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
    overwrite: bool = False  # replace what is there (bigquery / asset)
    append: bool = False  # BigQuery: add to an existing table
    ingestion_time_parameters: dict[str, Any] | None = (
        None  # FeatureView serving options, passed as one ee dict
    )
    # Scoped and validated like the sibling options: a free `str` constructed
    # happily and was then never passed to `toAsset` / `toBigQuery` /
    # `toFeatureView`, i.e. silently ignored where `overwrite` would have
    # raised. The default is kept rather than made `None`, so the attribute
    # still reads back as the format Earth Engine would use.
    file_format: Literal["CSV", "GeoJSON", "KML", "KMZ", "SHP", "TFRecord"] = (
        "CSV"  # file destinations only (see `_FILE_DESTINATIONS`)
    )
    wait_for_export: bool = False  # block until the task finishes

    @model_validator(mode="after")
    def _consistent(self) -> TableSink:
        """Refuse option/destination mismatches and a missing address.

        Returns:
            The config unchanged once it is consistent.

        Raises:
            ValueError: If `overwrite` and `append` are both set, `overwrite`
                is set outside `_OVERWRITE_DESTINATIONS`, `append` outside
                `_APPEND_DESTINATIONS`, `ingestion_time_parameters`
                is set for a non-FeatureView destination, `file_format` is set
                for a destination that writes no file, the BigQuery `table` is
                not `project.dataset.table`-shaped, or the destination's
                required address field is `None`.
        """
        if self.file_format != "CSV" and self.destination not in _FILE_DESTINATIONS:
            raise ValueError(
                f"file_format={self.file_format!r} is only passed on by the "
                f"file destinations {sorted(_FILE_DESTINATIONS)}; the "
                f"destination is {self.destination!r}, which writes a table "
                "rather than a file and would ignore it"
            )
        if (
            self.destination == "bigquery"
            and self.table is not None
            and _BIGQUERY_TABLE_RE.fullmatch(self.table) is None
        ):
            raise ValueError(
                f"table={self.table!r} is not a BigQuery destination; Earth "
                "Engine needs the fully qualified "
                "'project.dataset.table' form"
            )
        if self.overwrite and self.append:
            raise ValueError("overwrite and append are mutually exclusive")
        if self.overwrite and self.destination not in _OVERWRITE_DESTINATIONS:
            raise ValueError(
                "overwrite is taken by the "
                f"{sorted(_OVERWRITE_DESTINATIONS)} destinations; the "
                f"destination is {self.destination!r}, whose "
                "Export.table factory has no overwrite parameter"
            )
        if self.append and self.destination not in _APPEND_DESTINATIONS:
            raise ValueError(
                "append is a BigQuery write disposition; the destination is "
                f"{self.destination!r}"
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
                collection=features,
                description=description,
                assetId=self.asset_id,
                overwrite=self.overwrite,
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
