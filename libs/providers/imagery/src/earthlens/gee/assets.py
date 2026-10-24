"""`AssetManager`: the Earth Engine asset lifecycle as its own control-plane object.

The `GEE` backend fetches imagery; it deliberately has no verbs for *managing*
assets. The `export_via="asset"` sink creates them, ingestion manifests import
external rasters and tables into them, and nothing in earthlens could list,
inspect, move, secure or delete any of it. That lifecycle is a distinct
concern from reading pixels, so it lives on its own object rather than as
methods bolted onto the imagery facade: an explicitly instantiated
`AssetManager(project=...)` makes the control-plane intent — including the
destructive `delete` / `copy` / `set_acl` — obvious at the call site.

Every method is a thin, named wrapper over the `ee.data` asset family
(`createFolder` / `createAsset` / `listAssets` / `getAsset` / `deleteAsset` /
`copyAsset` / `renameAsset` / `setAssetProperties` / `getAssetAcl` /
`setAssetAcl` / `startIngestion` / `startTableIngestion`). Reads come back as
frozen `AssetInfo` rows — one readable line per asset instead of a raw dict —
and ingestion submissions come back as the same `TaskInfo` the
`earthlens.gee.jobs` machinery tracks by id, exactly like the batch export
sinks. The project resolves the way `jobs` resolves it: an explicit
`project=` wins, otherwise the one the Earth Engine SDK was initialised with.

Asset management is pure `ee.data` control-plane, unrelated to the pixel
engines: it is server-side regardless of any request's `engine=`, and there
is nothing here for the EEDAI reader to decline.

Examples:
    - A manager is a frozen value object, constructed without Earth Engine:
        ```python
        >>> from earthlens.gee import AssetManager
        >>> manager = AssetManager(project="my-project")
        >>> manager.root
        'projects/my-project/assets'

        ```
    - Create, list and delete a folder (needs an initialised `ee`):
        ```python
        >>> from earthlens.gee import AssetManager  # doctest: +SKIP
        >>> manager = AssetManager(project="my-project")  # doctest: +SKIP
        >>> manager.create_folder("projects/my-project/assets/scratch")  # doctest: +SKIP
        >>> for row in manager.list_assets():  # doctest: +SKIP
        ...     print(row)
        >>> manager.delete("projects/my-project/assets/scratch")  # doctest: +SKIP

        ```
"""

from __future__ import annotations

from typing import Any

import ee
from pydantic import BaseModel, ConfigDict

from earthlens.base import SummarisedLeaf
from earthlens.gee.jobs import TaskInfo, _resolve_project, get_task_status

__all__ = ["AssetInfo", "AssetManager"]


class AssetInfo(SummarisedLeaf):
    """One Earth Engine asset, as a one-line row instead of a raw dict.

    Frozen view of the fields a listing is actually read for: what the asset
    is called, what kind it is, and how big it is. The raw `ee.data` payload
    spells the identifier as `id` or `name` depending on the endpoint; both
    normalise onto `id` here.

    Examples:
        - A row prints as one line, skipping what it does not know:
            ```python
            >>> from earthlens.gee.assets import AssetInfo
            >>> print(AssetInfo(id="projects/p/assets/f", type="FOLDER"))
            AssetInfo(projects/p/assets/f, FOLDER)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _summary_fields = ("id", "type", "size_bytes")

    id: str  # the asset id, e.g. `projects/<p>/assets/<path>`
    type: str | None = None  # `IMAGE` / `IMAGE_COLLECTION` / `TABLE` / `FOLDER`
    size_bytes: int | None = None  # storage size, when the endpoint reports one


def _to_asset_info(payload: dict[str, Any]) -> AssetInfo:
    """Normalise one raw `ee.data` asset dict into an `AssetInfo`.

    Args:
        payload: An asset description as `getAsset` / `listAssets` /
            `createAsset` return it.

    Returns:
        The parsed row, with `sizeBytes` (a string on the wire) as an int.
    """
    size = payload.get("sizeBytes")
    return AssetInfo(
        id=str(payload.get("id") or payload.get("name") or ""),
        type=payload.get("type"),
        size_bytes=int(size) if size is not None else None,
    )


def _request_id(request_id: str | None) -> str:
    """Return the caller's ingestion request id, or mint one.

    Args:
        request_id: The caller's id, or `None` to mint one via
            `ee.data.newTaskId` — Earth Engine's own source of unique,
            safely-retryable ids.

    Returns:
        The id to submit the ingestion under.
    """
    return request_id if request_id is not None else str(ee.data.newTaskId()[0])


class AssetManager(BaseModel):
    """Create, list, inspect, move, secure and ingest Earth Engine assets.

    A frozen value object holding only the Cloud `project` (resolved like the
    `earthlens.gee.jobs` trackers when left `None`), with one thin method per
    `ee.data` asset-lifecycle call. It needs an initialised `ee` and nothing
    else — no `GEE` backend, no request shape — because managing assets is
    control-plane work, server-side regardless of any download's `engine=`.

    `delete`, `copy` (with `overwrite=True`) and `set_acl` are destructive or
    security-sensitive: they forward to Earth Engine exactly as given, so the
    call site carries the intent.

    Examples:
        - The manager's configuration cannot drift after construction:
            ```python
            >>> from earthlens.gee import AssetManager
            >>> manager = AssetManager(project="my-project")
            >>> manager.project = "other"
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str | None = None  # Cloud project; None resolves from the SDK

    @property
    def root(self) -> str:
        """The project's asset root folder, `projects/<project>/assets`.

        Returns:
            The folder id that `list_assets` defaults to.

        Raises:
            RuntimeError: If `project` is `None` and the current Earth Engine
                project cannot be resolved from the SDK.
        """
        return f"projects/{_resolve_project(self.project)}/assets"

    def create_folder(self, path: str) -> AssetInfo:
        """Create an asset folder.

        Args:
            path: The folder's asset id, e.g. `projects/<p>/assets/scratch`.

        Returns:
            The created folder, parsed from `ee.data.createFolder`'s answer.
        """
        return _to_asset_info(ee.data.createFolder(path))

    def create_asset(
        self,
        path: str,
        asset_type: str = "IMAGE_COLLECTION",
        properties: dict[str, Any] | None = None,
    ) -> AssetInfo:
        """Create an empty container asset — an image collection or a folder.

        Args:
            path: The new asset's id.
            asset_type: The Cloud API type, `IMAGE_COLLECTION` (the default)
                or `FOLDER`.
            properties: Optional metadata properties to set at creation.

        Returns:
            The created asset, parsed from `ee.data.createAsset`'s answer.
        """
        return _to_asset_info(
            ee.data.createAsset({"type": asset_type}, path, properties)
        )

    def list_assets(self, parent: str | None = None) -> list[AssetInfo]:
        """List the assets directly under a folder or collection.

        Args:
            parent: The folder / collection id to list; `None` lists the
                project's asset `root`.

        Returns:
            One parsed `AssetInfo` per child, empty when the container is.
        """
        target = parent if parent is not None else self.root
        response = ee.data.listAssets({"parent": target})
        return [_to_asset_info(row) for row in response.get("assets", [])]

    def get_asset(self, asset_id: str) -> AssetInfo:
        """Fetch one asset's description.

        Args:
            asset_id: The asset to inspect.

        Returns:
            The parsed `AssetInfo` row.
        """
        return _to_asset_info(ee.data.getAsset(asset_id))

    def delete(self, asset_id: str) -> None:
        """Delete an asset. Destructive and immediate — there is no undo.

        Args:
            asset_id: The asset to delete.
        """
        ee.data.deleteAsset(asset_id)

    def copy_asset(
        self, source_id: str, destination_id: str, *, overwrite: bool = False
    ) -> None:
        """Copy an asset.

        Named `copy_asset` rather than the `ee.data`-matching `copy` because
        pydantic's `BaseModel` already defines a `copy` with an incompatible
        signature.

        Args:
            source_id: The asset to copy.
            destination_id: The id of the copy.
            overwrite: Whether an existing asset at the destination may be
                replaced.
        """
        ee.data.copyAsset(source_id, destination_id, overwrite)

    def rename(self, source_id: str, destination_id: str) -> None:
        """Rename (move) an asset.

        Args:
            source_id: The asset to move.
            destination_id: Its new id.
        """
        ee.data.renameAsset(source_id, destination_id)

    def update_properties(self, asset_id: str, properties: dict[str, Any]) -> None:
        """Set metadata properties on an asset (a `None` value deletes one).

        Args:
            asset_id: The asset to update.
            properties: The property keys and values to set.
        """
        ee.data.setAssetProperties(asset_id, properties)

    def get_acl(self, asset_id: str) -> dict[str, Any]:
        """Fetch an asset's access control list.

        Args:
            asset_id: The asset to inspect.

        Returns:
            The ACL dict as Earth Engine reports it (`owners` / `writers` /
            `readers` / `all_users_can_read`).
        """
        return dict(ee.data.getAssetAcl(asset_id))

    def set_acl(self, asset_id: str, acl: dict[str, Any]) -> None:
        """Set an asset's access control list.

        Earth Engine merges the owner entries of the old ACL into the update,
        so owners cannot be dropped this way.

        Args:
            asset_id: The asset to secure.
            acl: The ACL update dict.
        """
        ee.data.setAssetAcl(asset_id, acl)

    def start_ingestion(
        self,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
        allow_overwrite: bool = False,
    ) -> TaskInfo:
        """Submit an image ingestion manifest and return its tracked task.

        Args:
            params: The image manifest — destination `name`, `tilesets` of
                `gs://` source URIs, optional `bands`.
            request_id: A unique id making the submission safely retryable;
                minted via `ee.data.newTaskId` when omitted.
            allow_overwrite: Whether the ingested image may overwrite an
                existing asset.

        Returns:
            The queued task, normalised exactly like the batch export sinks'
            — poll it with `earthlens.gee.jobs.get_task_status` /
            `wait_for_task_id`.
        """
        submitted = ee.data.startIngestion(
            _request_id(request_id), params, allow_overwrite
        )
        return self._submitted_task(submitted)

    def start_table_ingestion(
        self,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
        allow_overwrite: bool = False,
    ) -> TaskInfo:
        """Submit a table ingestion manifest and return its tracked task.

        Args:
            params: The table manifest — destination `name` and `sources` of
                `gs://` file URIs with optional charsets.
            request_id: A unique id making the submission safely retryable;
                minted via `ee.data.newTaskId` when omitted.
            allow_overwrite: Whether the ingested table may overwrite an
                existing asset.

        Returns:
            The queued task, normalised exactly like the batch export sinks'.
        """
        submitted = ee.data.startTableIngestion(
            _request_id(request_id), params, allow_overwrite
        )
        return self._submitted_task(submitted)

    def _submitted_task(self, submitted: dict[str, Any]) -> TaskInfo:
        """Resolve a just-submitted ingestion into the jobs `TaskInfo` shape.

        `startIngestion` / `startTableIngestion` answer with the operation
        `name` (and the task `id`), not a task status, so the status is
        fetched once through the same `get_task_status` path every other
        export rides. That one extra `getOperation` is the price of handing
        back the same normalised `TaskInfo` the image and table sinks do,
        rather than a second, submission-only shape; `name` is already a full
        `projects/.../operations/<id>` so no project lookup is needed.

        Args:
            submitted: The submission response dict.

        Returns:
            The normalised `TaskInfo` for the queued ingestion.

        Raises:
            ValueError: If the response carries neither `name` nor `id`, which
                left a bare `KeyError` where the docstring implies a
                normalised failure.
        """
        handle = submitted.get("name") or submitted.get("id")
        if handle is None:
            raise ValueError(
                "the ingestion response carries neither 'name' nor 'id', so "
                f"the queued operation cannot be tracked; got keys "
                f"{sorted(submitted)}"
            )
        return get_task_status(str(handle), project=self.project)
