"""Validated configuration the strategy objects read, instead of the backend.

`GEE.__init__` sets around twenty-five instance attributes, and the `Engine`,
`Sink` and `WorkUnit` strategies each need a slice of them. Handing a strategy
`self` would couple it to the whole backend — `DriveSink` could reach
`cloud_mask`, which is none of its business — and threading loose keywords would
just rebuild the wide signature one layer down.

Two cohesive frozen value objects give each strategy a typed, validated slice:
:class:`ExportConfig` is *where the pixels go*, :class:`EngineConfig` is *how
they are computed*. The same move `AggregationConfig` already made in core.

One thing deliberately **not** done here: `crs` and `scale` are not carried as
raw fields on both objects. Duplicating the grid inputs while leaving the
derivation on the backend is how the god-class survives a refactor. They live on
:class:`EngineConfig` once, and :class:`~earthlens.gee.grid.OutputGrid` owns
turning them into a grid; a sink that needs them for an Earth Engine export call
reads the pair back through :attr:`EngineConfig.grid_inputs`.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import ConfigDict, Field, model_validator

from earthlens.base.leaves import SummarisedLeaf

__all__ = ["ExportConfig", "EngineConfig"]

#: Default timeout for the synchronous `getDownloadURL` fetch, in seconds.
DEFAULT_HTTP_TIMEOUT_S: float = 300.0


class ExportConfig(SummarisedLeaf):
    """Where a request's pixels go, and in what form.

    Attributes:
        export_via: Which sink writes the result.
        drive_folder: Google Drive folder, required by `export_via="drive"`.
        gcs_bucket: Cloud Storage bucket, required by `export_via="gcs"`.
        asset_id: Parent folder asset id, required by `export_via="asset"`.
        cog: Write the EEDAI path's raster as a Cloud Optimized GeoTIFF.
        resample: Resampling kernel the EEDAI reader warps the native grid with.
        auto_split: Tile and mosaic around Earth Engine's per-axis cap.
        http_timeout: Timeout for the synchronous `getDownloadURL` fetch.
        wait_for_export: Block until an asynchronous task reaches a terminal
            state, rather than returning a `TaskInfo` at submission.
        materialize: Return the in-memory pyramids object alongside the written
            file, as `DownloadResult.objects`, rather than only a path. Needs a
            sink that writes pixels locally — a batch export has nothing to
            hand back.
        nodata: The fill value to declare on the written raster. Lets the caller
            state it rather than inheriting whatever the driver does or does not
            declare — the configurable half of the no-fill-value divergence.
        dtype: The output data type, when the caller needs to pin it.

    Examples:
        - The default is the synchronous path, which needs no destination:
            ```python
            >>> config = ExportConfig()
            >>> config.export_via, config.http_timeout
            ('url', 300.0)

            ```
        - A sink's prerequisite is checked at construction, long before the
          credential build and the catalog query a download would pay for:
            ```python
            >>> ExportConfig(export_via="gcs")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ExportConfig
            ...
            >>> ExportConfig(export_via="gcs", gcs_bucket="my-bucket").gcs_bucket
            'my-bucket'

            ```
        - It is frozen, so a sink cannot rewrite the config it was handed:
            ```python
            >>> config = ExportConfig(cog=True)
            >>> config.cog = False
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ExportConfig
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    _summary_fields: ClassVar[tuple[str, ...]] = (
        "export_via",
        "cog",
        "resample",
        "materialize",
    )

    export_via: Literal["url", "drive", "gcs", "asset"] = "url"
    drive_folder: str | None = None
    gcs_bucket: str | None = None
    asset_id: str | None = None
    cog: bool = False
    resample: str = "nearest"
    auto_split: bool = False
    http_timeout: float = Field(default=DEFAULT_HTTP_TIMEOUT_S, gt=0)
    wait_for_export: bool = True
    materialize: bool = False
    # Read only by `PixelsEngine.request`, whose fetch path is not wired yet,
    # so there is deliberately no `GEE(nodata=...)` constructor kwarg: a public
    # argument that silently does nothing is worse than its absence. The fields
    # stay because the request builder is implemented and tested against them.
    nodata: float | int | None = None
    dtype: str | None = None

    @model_validator(mode="after")
    def _require_the_sinks_prerequisite(self) -> ExportConfig:
        """Reject a sink without the destination it cannot work without.

        Each asynchronous sink needs exactly one extra field, and finding that
        out at download time — after the credential build and the catalog
        query — wastes the whole setup. The messages are the backend's own,
        preserved verbatim.

        Returns:
            The validated config.

        Raises:
            ValueError: If the chosen sink's required field is missing.
        """
        required = {
            "drive": ("drive_folder", "export_via='drive' requires drive_folder="),
            "gcs": ("gcs_bucket", "export_via='gcs' requires gcs_bucket="),
            "asset": (
                "asset_id",
                "export_via='asset' requires asset_id= (the parent folder "
                "asset, e.g. 'projects/my-project/assets/my-folder')",
            ),
        }
        if self.export_via in required:
            field, message = required[self.export_via]
            if not getattr(self, field):
                raise ValueError(message)
        return self


class EngineConfig(SummarisedLeaf):
    """How a request's pixels are computed, and by which engine.

    Attributes:
        engine: Which layer materialises the pixels.
        reducer: The temporal reducer — a name, a `percentile:<q>` spec, or a
            resolved `Reducer`. `None` falls back to the dataset's own default.
        crs: The output CRS. Held here once; `OutputGrid` turns it into a grid.
        scale: Target ground sample distance in metres, or `None` to read at the
            asset's native resolution.
        cloud_mask: Optional per-image hook, `.map`-applied before the reducer.
            Despite the name it is a general `ee.Image -> ee.Image` operation.
        filters: Collection-level hooks, applied left to right.
        property_filter: An OGR attribute-filter string narrowing collection
            scenes on the EEDAI path; Earth Engine ignores it.
        region: The cutline to clip to, or `None`.
        discover_extent: Fall back to an Earth Engine-side extent query when the
            catalog row's dates are missing.
        high_volume: Ask for Earth Engine's high-volume endpoint, read by the
            engines that can use it (see
            :meth:`~earthlens.gee.pixels.PixelsEngine.endpoint`). Less caching,
            and complex queries cost more EECU-time there, so it is opted into
            per request rather than switched on globally.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    _summary_fields: ClassVar[tuple[str, ...]] = ("engine", "reducer", "crs", "scale")

    engine: str = "auto"
    reducer: Any = None
    crs: str = "EPSG:4326"
    scale: float | None = None
    cloud_mask: Any = None
    filters: tuple[Any, ...] = ()
    property_filter: str | None = None
    region: Any = None
    discover_extent: bool = False
    # Read only by `PixelsEngine.endpoint`, whose fetch path is not wired, so
    # there is no `GEE(high_volume=...)` kwarg yet - same reasoning as `nodata`.
    high_volume: bool = False

    @property
    def needs_server_side_compute(self) -> bool:
        """Whether anything server-side has to shape the image.

        The single question the EEDAI reader's eligibility turns on: it
        materialises stored pixels and cannot execute a computation graph, so a
        request that needs one belongs on an Earth Engine engine.

        Returns:
            `True` when a `cloud_mask` or any `filters` were given.

        Examples:
            - A plain request needs nothing computed, so the reader may serve
              it:
                ```python
                >>> EngineConfig(scale=30.0).needs_server_side_compute
                False

                ```
            - One collection filter is enough to make it an Earth Engine job:
                ```python
                >>> config = EngineConfig(filters=(lambda c: c.limit(10),))
                >>> config.needs_server_side_compute
                True

                ```
        """
        return self.cloud_mask is not None or bool(self.filters)

    @property
    def grid_inputs(self) -> tuple[str, float | None]:
        """Return `(crs, scale)`, the inputs `OutputGrid` is built from.

        Exposed as a pair so a sink needing them for an Earth Engine export call
        reads them from one place rather than carrying its own copies.

        Returns:
            The output CRS and the metre scale, which is `None` when the read
            should happen at the asset's native resolution.

        Examples:
            - The pair reads back as given, ready to unpack into a grid
              constructor:
                ```python
                >>> crs, scale = EngineConfig(crs="EPSG:3857", scale=30.0).grid_inputs
                >>> crs, scale
                ('EPSG:3857', 30.0)

                ```
            - Without a `scale` the second half is `None`, which the engines
              read as "use the asset's native resolution":
                ```python
                >>> EngineConfig().grid_inputs
                ('EPSG:4326', None)

                ```
        """
        return self.crs, self.scale
