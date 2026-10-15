"""Where a finished image is written, as strategy objects.

`_export_via_batch` branched on `export_via` to pick which
`ee.batch.Export.image.to*` to call and how to spell the destination, and
`__init__` separately checked each sink's prerequisite. One sink per object
instead, so a table or a video destination is a new subclass rather than two
more branches in the same `if/elif`.

Each sink states its own prerequisite through `Sink.required_config`, which
is what `ExportConfig`'s validator checks, so the
requirement lives next to the sink that has it.

As with `earthlens.gee.engines`, the *decision* is here and the *mechanics*
are called back into the backend, so resolving a sink changes no observable
behaviour. `Sink.write` is where that decision is made: `GEE._api` hands
the composited image to the resolved sink instead of branching on `export_via`,
and each sink calls the transport it needs - which is also where the
synchronous sink chooses between `getDownloadURL` and the pixel API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from earthlens.gee.backend import GEE
    from earthlens.gee.catalog import Dataset
    from earthlens.gee.jobs import TaskInfo

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
    """One destination: how to address it, how to write to it, how it reads back.

    The sink owns the *decision* and calls the backend for the *mechanics*, the
    same split `earthlens.gee.engines` uses. `write` is what `_api`
    calls, so adding a destination is a subclass plus a registry entry rather
    than another branch in the export path.

    Attributes:
        name: The registry key, and the value a user's `export_via=` resolves
            from.
        required_config: The field this sink cannot work without, or `None` when
            it needs nothing extra. Named once and read three times:
            `validate_config` refuses a request that left it unset, the
            `ExportConfig` validator does the same for a config built directly,
            and a batch sink reads the value off the backend under this name
            when it addresses its destination.
        prerequisite_hint: The parenthesised hint appended to the refusal, for a
            prerequisite whose shape is not obvious from its name.
        is_batch: Whether this sink queues an asynchronous Earth Engine task
            rather than fetching pixels synchronously. `True` for every sink but
            `UrlSink`.
    """

    name: ClassVar[str]
    required_config: ClassVar[str | None] = None
    prerequisite_hint: ClassVar[str] = ""
    is_batch: ClassVar[bool] = True
    #: Whether this sink writes a GeoTIFF whose format options Earth Engine
    #: accepts, which is the only place its API takes a fill value
    #: (`GeoTiffImageExportOptions.noData`). `False` for the synchronous sink,
    #: which writes the file here rather than server-side, and for the asset
    #: sink, which creates an asset and has no file format at all.
    takes_format_options: ClassVar[bool] = False

    @abstractmethod
    def write(
        self,
        backend: GEE,
        image: Any,
        *,
        var_info: Dataset,
        scale: float,
        bands: Sequence[str],
        region: Any,
        prefix: str,
    ) -> Path | str | TaskInfo:
        """Write one composited image to this destination.

        Args:
            backend: The `GEE` instance, for its config and the transports.
            image: The composited `ee.Image` for this bucket.
            var_info: The catalog entry being read.
            scale: The resolved ground sample distance in metres.
            bands: The bands the read asked for, in order.
            region: The `ee.Geometry` to clip to.
            prefix: The filename (or asset) stem for this bucket.

        Returns:
            A `Path` for a synchronous sink, else the destination string, or a
            `TaskInfo` when the export was submitted
            without waiting.
        """

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> None:
        """Refuse this sink if the destination it needs was not supplied.

        Takes the prerequisite values rather than the whole backend, so it runs
        in `GEE.__init__` before anything else is built - which is the point: a
        `"drive"` export with no folder has to fail before the catalog parse,
        not after a run has queued its first task.

        Args:
            config: The candidate prerequisite values, keyed as the constructor
                spells them (`drive_folder`, `gcs_bucket`, `asset_id`).

        Raises:
            ValueError: If this sink's `required_config` is missing or empty.
        """
        if cls.required_config is None:
            return
        if not config.get(cls.required_config):
            raise ValueError(
                f"export_via={cls.name!r} requires "
                f"{cls.required_config}={cls.prerequisite_hint}".rstrip()
            )

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

    def write(
        self,
        backend: GEE,
        image: Any,
        *,
        var_info: Dataset,
        scale: float,
        bands: Sequence[str],
        region: Any,
        prefix: str,
    ) -> Path:
        """Fetch the pixels now, through whichever engine was resolved.

        The one sink `engine=` means anything to, which is why the choice
        between Earth Engine's two pull APIs is made here: `computePixels`
        returns the GeoTIFF bytes directly from an explicit grid, and
        `getDownloadURL` is the historical zip-over-HTTP path. Both evaluate the
        same graph and both are bound by Earth Engine's 32768-px per-axis cap,
        so the choice is about transport, not about what can be asked for.

        Args:
            backend: The `GEE` instance, for the engine and the transports.
            image: The composited `ee.Image`.
            var_info: The catalog entry being read.
            scale: Ground sample distance in metres.
            bands: The bands the read asked for. Only the pixel API selects
                bands on the request; `getDownloadURL` takes them from `image`.
            region: The `ee.Geometry` to clip to.
            prefix: The filename stem.

        Returns:
            The written GeoTIFF's path.

        Raises:
            ValueError: If the request exceeds the per-axis cap and
                `auto_split` is off, or no scale can be resolved.
            RuntimeError: If Earth Engine returns a zip instead of a GeoTIFF on
                the `getDownloadURL` path.
        """
        from earthlens.gee.pixels import PixelsEngine

        if backend._engine.name == PixelsEngine.name:
            return backend._export_via_pixels(image, var_info, scale, bands, prefix)
        return backend._export_via_url(image, var_info, scale, region, prefix)


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
    takes_format_options: ClassVar[bool] = True

    def write(
        self,
        backend: GEE,
        image: Any,
        *,
        var_info: Dataset,
        scale: float,
        bands: Sequence[str],
        region: Any,
        prefix: str,
    ) -> str | TaskInfo:
        """Queue the export and return its destination, or its task.

        `var_info` and `bands` are unused: a queued export names its own output
        and Earth Engine selects the bands server-side from the image it was
        handed. They stay in the signature because every sink is called the same
        way, which is what lets `_api` stop branching.

        Args:
            backend: The `GEE` instance, for the export mechanics.
            image: The composited `ee.Image`.
            var_info: Unused.
            scale: Ground sample distance in metres.
            bands: Unused.
            region: The `ee.Geometry` to clip to.
            prefix: The export's description and filename stem.

        Returns:
            The destination string, or a `TaskInfo` when `wait_for_export` is
            `False`.
        """
        del var_info, bands
        return backend._export_via_batch(image, scale, region, prefix)

    def target(self, backend: GEE) -> dict[str, str]:
        """Return the keyword arguments that address this sink's destination.

        Args:
            backend: The `GEE` instance, for the configured target.

        Returns:
            The `to*` keyword arguments — the target plus `fileNamePrefix` for
            the file-based sinks, which `AssetSink` overrides.
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
    # An asset is not a file, so it carries no `fileFormat` and no format
    # options - which is why `nodata=` cannot reach this one.
    takes_format_options: ClassVar[bool] = False
    prerequisite_hint: ClassVar[str] = (
        "(the parent folder asset, e.g. 'projects/my-project/assets/my-folder')"
    )
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
