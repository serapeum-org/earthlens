"""Where a finished image is written, as strategy objects.

`_export_via_batch` branched on `export_via` to pick which
`ee.batch.Export.image.to*` to call and how to spell the destination, and
`__init__` separately checked each sink's prerequisite. One sink per object
instead, so a table or a video destination is a new subclass rather than two
more branches in the same `if/elif`.

Each sink states its own prerequisite through :attr:`Sink.required_config`, which
is what :class:`~earthlens.gee.configs.ExportConfig`'s validator checks, so the
requirement lives next to the sink that has it.

As with :mod:`earthlens.gee.engines`, the *decision* is here and the *mechanics*
are called back into the backend, so resolving a sink changes no observable
behaviour.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.backend import GEE

__all__ = [
    "Sink",
    "UrlSink",
    "DriveSink",
    "GcsSink",
    "AssetSink",
    "SINKS",
    "resolve_sink",
]


class Sink(ABC):
    """Describes one destination: how to address it and how it reads back.

    Deliberately declarative. The mechanics of writing still live on the backend
    (`_export_via_url` / `_export_via_batch`), which now reads *which* export
    method, *which* target keyword and *what* destination from the resolved sink
    instead of branching on the `export_via` string. There is no `write()`
    method: the payload a sink would write is a
    :class:`~earthlens.gee.work.WorkUnit`, but the backend still composites and
    writes it, so a `write()` here would be a seam nothing calls.

    Attributes:
        name: The registry key, and the value a user's `export_via=` resolves
            from.
        required_config: The field this sink cannot work without, or `None` when
            it needs nothing extra. Named once and read twice: the
            `ExportConfig` validator rejects a request that left it unset, and a
            batch sink reads the value off the backend under the same name when
            it addresses its destination.
        is_batch: Whether this sink queues an asynchronous Earth Engine task
            rather than fetching pixels synchronously. `True` for every sink but
            :class:`UrlSink`.
    """

    name: ClassVar[str]
    required_config: ClassVar[str | None] = None
    is_batch: ClassVar[bool] = True

    def destination(self, backend: GEE, prefix: str) -> str:
        """Return the destination string this sink reports on success.

        Args:
            backend: The `GEE` instance, for the configured target.
            prefix: The filename stem.

        Returns:
            The destination, e.g. `drive://<folder>/<prefix>`.

        Raises:
            NotImplementedError: For a synchronous sink, which returns a path.
        """
        raise NotImplementedError(f"{type(self).__name__} writes a path, not a URI")

    def __repr__(self) -> str:
        """Return the sink's class name, for log lines."""
        return f"{type(self).__name__}()"


class UrlSink(Sink):
    """Synchronous `getDownloadURL`, or the EEDAI reader, writing to disk.

    The only sink that returns a `Path`: it fetches the pixels now rather than
    queueing work. It is also the only sink the EEDAI reader participates in,
    which is why `engine=` is ignored for the other three.
    """

    name: ClassVar[str] = "url"
    is_batch: ClassVar[bool] = False


class _BatchSink(Sink):
    """An asynchronous `ee.batch.Export.image.to*` sink.

    The three batch sinks differ only in which constructor they call, which
    keyword addresses the target, and how the destination reads — so that is all
    a subclass declares.

    Attributes:
        export_method: The `ee.batch.Export.image` method name.
        target_kwarg: The keyword that addresses the destination.
        scheme: The destination URI scheme.
    """

    export_method: ClassVar[str]
    target_kwarg: ClassVar[str]
    scheme: ClassVar[str]

    def target(self, backend: GEE) -> dict[str, str]:
        """Return the keyword arguments that address this sink's destination.

        Args:
            backend: The `GEE` instance, for the configured target.

        Returns:
            The `to*` keyword arguments — the target plus `fileNamePrefix` for
            the file-based sinks, which :class:`AssetSink` overrides.
        """
        assert self.required_config is not None  # every batch sink declares one
        return {self.target_kwarg: getattr(backend, self.required_config)}


class DriveSink(_BatchSink):
    """`ee.batch.Export.image.toDrive`, into a named Drive folder."""

    name: ClassVar[str] = "drive"
    required_config: ClassVar[str | None] = "drive_folder"
    export_method: ClassVar[str] = "toDrive"
    target_kwarg: ClassVar[str] = "folder"
    scheme: ClassVar[str] = "drive"

    def destination(self, backend: GEE, prefix: str) -> str:
        """Return `drive://<folder>/<prefix>`.

        Args:
            backend: The `GEE` instance, for the configured folder.
            prefix: The filename stem.

        Returns:
            The Drive destination.
        """
        return f"drive://{backend.drive_folder}/{prefix}"


class GcsSink(_BatchSink):
    """`ee.batch.Export.image.toCloudStorage`, into a GCS bucket."""

    name: ClassVar[str] = "gcs"
    required_config: ClassVar[str | None] = "gcs_bucket"
    export_method: ClassVar[str] = "toCloudStorage"
    target_kwarg: ClassVar[str] = "bucket"
    scheme: ClassVar[str] = "gs"

    def destination(self, backend: GEE, prefix: str) -> str:
        """Return `gs://<bucket>/<prefix>`.

        Args:
            backend: The `GEE` instance, for the configured bucket.
            prefix: The filename stem.

        Returns:
            The Cloud Storage destination.
        """
        return f"gs://{backend.gcs_bucket}/{prefix}"


class AssetSink(_BatchSink):
    """`ee.batch.Export.image.toAsset`, creating one asset per export.

    The only batch sink that addresses its target with `assetId` rather than
    `fileNamePrefix`, because each export creates an asset at
    `<asset_id>/<prefix>` rather than a file with a name prefix.
    """

    name: ClassVar[str] = "asset"
    required_config: ClassVar[str | None] = "asset_id"
    export_method: ClassVar[str] = "toAsset"
    target_kwarg: ClassVar[str] = "assetId"
    scheme: ClassVar[str] = "ee"

    def target_asset(self, backend: GEE, prefix: str) -> str:
        """Return the asset path this export creates.

        Args:
            backend: The `GEE` instance, for the configured parent folder.
            prefix: The filename stem.

        Returns:
            `<asset_id>/<prefix>`, with no doubled separator.
        """
        assert backend.asset_id is not None  # the config validator requires it
        return f"{backend.asset_id.rstrip('/')}/{prefix}"

    def target(self, backend: GEE, prefix: str = "") -> dict[str, str]:
        """Return `assetId`, and no `fileNamePrefix`.

        Args:
            backend: The `GEE` instance.
            prefix: The filename stem, which becomes part of the asset path.

        Returns:
            The single `assetId` keyword.
        """
        return {self.target_kwarg: self.target_asset(backend, prefix)}

    def destination(self, backend: GEE, prefix: str) -> str:
        """Return `ee://<asset_id>/<prefix>`.

        Args:
            backend: The `GEE` instance.
            prefix: The filename stem.

        Returns:
            The asset destination.
        """
        return f"ee://{self.target_asset(backend, prefix)}"


#: Every sink addressable by `export_via=`.
SINKS: dict[str, type[Sink]] = {
    cls.name: cls for cls in (UrlSink, DriveSink, GcsSink, AssetSink)
}


def resolve_sink(spec: str | Sink) -> Sink:
    """Resolve a sink name, or pass an instance through.

    Args:
        spec: A registry name (`"url"`, `"drive"`, `"gcs"`, `"asset"`) or a
            `Sink`.

    Returns:
        The resolved sink.

    Raises:
        ValueError: If the name is not registered.
        TypeError: If `spec` is neither a string nor a `Sink`.

    Examples:
        - Each shipped name resolves:
            ```python
            >>> resolve_sink("drive").required_config
            'drive_folder'

            ```
        - The synchronous sink needs nothing extra:
            ```python
            >>> resolve_sink("url").required_config is None
            True

            ```
    """
    if isinstance(spec, Sink):
        return spec
    if not isinstance(spec, str):
        raise TypeError(
            f"export_via must be a name or a Sink instance, got {type(spec).__name__}"
        )
    if spec not in SINKS:
        raise ValueError(f"export_via must be one of {sorted(SINKS)}, got {spec!r}")
    return SINKS[spec]()
