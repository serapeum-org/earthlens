"""Sentinel Hub server-side-render backend (raster or tabular output).

`SentinelHub(AbstractDataSource)` sends a **bbox/geometry + time + an evalscript**
to one of Sentinel Hub's request planes on CDSE (`sh.dataspace.copernicus.eu`,
free with a CDSE account); the **server computes on-the-fly** and earthlens
collects the result. Its closest siblings are the GEE and openEO backends — the
server does the band math / compositing / rendering and earthlens (a)
authenticates, (b) builds the request from a curated evalscript library, (c)
triggers, (d) collects the output.

A request is `variables={collection_or_recipe: [band, ...]}` plus a bbox + date
window. A key may name a **collection** (then an explicit `evalscript=` is
required) or an **evalscript recipe** (a bundled `.js` that pins its collection).
The plane is chosen by the `api=` kwarg (`"process"` / `"async"` / `"batch"` /
`"statistical"` / `"batch-statistical"`), auto-selected by request size +
whether a `geometry=` was supplied when `api=` is omitted.

Because it returns raster **or** tabular depending on `api=`, the backend is
`OUTPUT_KIND="mixed"` (a *fixed* class attribute, never mutated per-instance):
the `EarthLens` facade forwards `aggregate=` for `{"raster", "mixed"}`, and
`aggregate=` is supported on every plane (including the statistical plane, where
it maps to the Statistical `aggregation_interval`), so demoting the instance to
`"tabular"` would wrongly make the facade reject it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    CADENCE_ALIASES,
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    date_windows,
    safe_filename,
)
from earthlens.sentinel_hub._dispatch import resolve_api, validate_api
from earthlens.sentinel_hub._helpers import (
    ASYNC_MAX_DIMENSION,
    RASTER_APIS,
    SH_MAX_DIMENSION,
    cdse_collection,
    import_sentinelhub,
    interval_for,
    tile_bbox,
)
from earthlens.sentinel_hub.auth import SentinelHubAuth, SentinelHubCredentials
from earthlens.sentinel_hub.catalog import read_evalscript

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.sentinel_hub.catalog import Catalog, ResolvedRequest

#: The per-pixel scene-selection orders accepted by `mosaicking_order=`
#: (the `sentinelhub.MosaickingOrder` enum values).
_VALID_MOSAICKING_ORDERS: tuple[str, ...] = ("mostRecent", "leastRecent", "leastCC")

#: Default per-pixel scene selection passed to `SentinelHubRequest.input_data`.
_DEFAULT_MOSAICKING_ORDER = "mostRecent"


class SentinelHub(AbstractDataSource):
    """Server-side Sentinel Hub backend on CDSE (raster or tabular by `api=`).

    Attributes:
        OUTPUT_KIND: `"mixed"` — raster (Process/Async/Batch) or tabular
            (Statistical/Batch-Statistical) depending on the resolved `api=`.
            A *fixed* class attribute (never mutated per-instance) so the facade
            forwards `aggregate=` on every plane.
    """

    OUTPUT_KIND: OutputKind = "mixed"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        resolution: float = 10.0,
        evalscript: str | None = None,
        endpoint: str | None = None,
        mosaicking_order: str = _DEFAULT_MOSAICKING_ORDER,
        api: str | None = None,
        geometry: Any = None,
        maxcc: float | None = None,
        batch_output: dict[str, Any] | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        profile: str | None = None,
    ):
        """Initialise a Sentinel Hub backend instance.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            variables: `{collection_or_recipe_key: [band, ...]}`. A key is a
                catalog **collection** (needs an explicit `evalscript=`) or an
                **evalscript recipe** (pins its collection); an empty band list
                falls back to the recipe / collection default bands.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label; the render window is
                `start`/`end`.
            path: Output directory (created by the parent class).
            fmt: `strptime` format for `start` / `end`.
            resolution: Output pixel size in metres (→ `bbox_to_dimensions`).
            evalscript: A custom evalscript — an inline V3 JS string or a path to
                a `.js` file — that bypasses the recipe lookup (the collection
                then comes from the `variables` key). `None` uses the recipe's
                bundled `.js`.
            endpoint: Endpoint alias (`"cdse"`, `"commercial"`) or a full base
                URL. Defaults to CDSE-free.
            mosaicking_order: Per-pixel scene selection passed to
                `input_data` — `"mostRecent"` (default) / `"leastRecent"` /
                `"leastCC"` (the `MosaickingOrder` enum).
            api: The request plane (`"process"` / `"async"` / `"batch"` /
                `"statistical"` / `"batch-statistical"`), or `None` to
                auto-select by request size + whether `geometry=` was supplied.
            geometry: A shapely geometry / GeoJSON mapping / `FeatureCollection`
                for the Statistical planes (zonal stats over the polygon(s)).
            maxcc: Optional maximum cloud cover (0–1) passed to `input_data`
                (optical collections only).
            batch_output: S3 delivery spec (`{"bucket": ..., "iam_role_arn": ...}`)
                for the Batch planes.
            client_id: OAuth client id (else `SENTINELHUB_CLIENT_ID`).
            client_secret: OAuth client secret (else `SENTINELHUB_CLIENT_SECRET`).
            profile: A saved `SHConfig` profile name (else `SENTINELHUB_PROFILE`).

        Raises:
            ValueError: When `api` is an unknown plane, or `mosaicking_order` is
                not a recognised value.
        """
        validate_api(api)
        if mosaicking_order not in _VALID_MOSAICKING_ORDERS:
            raise ValueError(
                f"mosaicking_order must be one of {list(_VALID_MOSAICKING_ORDERS)}, "
                f"got {mosaicking_order!r}."
            )
        self._resolution = resolution
        self._evalscript = evalscript
        self._endpoint = endpoint
        self._mosaicking_order = mosaicking_order
        self._api_mode = api
        self._geometry = geometry
        self._maxcc = maxcc
        self._batch_output = batch_output
        env_creds = SentinelHubCredentials.from_env()
        self._credentials = SentinelHubCredentials(
            client_id=client_id or env_creds.client_id,
            client_secret=(
                SecretStr(client_secret) if client_secret else env_creds.client_secret
            ),
            profile=profile or env_creds.profile,
        )
        # Stored before super().__init__ because the base constructor calls
        # _initialize() (which needs the request) before it sets self.vars.
        self._variables = variables
        self._catalog: Catalog | None = None
        self._auth: SentinelHubAuth | None = None
        self._resolved: dict[str, ResolvedRequest] = {}
        # Aggregation request captured by download(); applied per plane.
        self._aggregate: AggregationConfig | None = None
        # Per-window time override set by the aggregate= render loop (C10).
        self._window_override: tuple[str, str] | None = None
        # Memoised resolved plane (deterministic from instance state; avoids a
        # second bbox_to_dimensions call across download() -> _fetch()).
        self._plane: str | None = None
        super().__init__(
            start=start,
            end=end,
            variables=variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self) -> None:
        """Load the catalog, resolve the requested keys, and build the auth.

        Returns `None` so the parent does not bind `self.client`; the auth
        wrapper is stored on `self._auth` and the resolved rows on
        `self._resolved`. Token minting is deferred to first request.

        Raises:
            TypeError: When `variables` is not a
                `{collection_or_recipe: [band, ...]}` mapping.
            ValueError: When `variables` is empty or names a key the catalog
                does not know (with a did-you-mean hint).
        """
        from earthlens.sentinel_hub.catalog import Catalog

        if not isinstance(self._variables, dict):
            raise TypeError(
                "Sentinel Hub requires variables as a "
                "{collection_or_recipe: [band, ...]} mapping, got "
                f"{type(self._variables).__name__}."
            )
        if not self._variables:
            raise ValueError(
                "Sentinel Hub requires variables={collection_or_recipe: "
                "[band, ...]} with at least one collection or recipe key."
            )
        self._catalog = Catalog()
        self._resolved = {key: self._catalog.resolve(key) for key in self._variables}
        self._auth = SentinelHubAuth(self._credentials, endpoint=self._endpoint)
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            temporal_resolution: Advisory cadence label (`"daily"`, `"monthly"`,
                `"hourly"`, `"yearly"`) → the pandas frequency on the extent.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: The parsed window (inclusive `end_date`).

        Raises:
            ValueError: When `temporal_resolution` is not one of the accepted
                cadences. A missing `start` / `end` is rejected earlier, by
                `AbstractDataSource._check_time_window` — the render needs a
                `time_interval`, so this backend keeps the inherited
                `REQUIRES_TIME_WINDOW = True`.
        """
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

    def _bbox(self) -> Any:
        """Build the Sentinel Hub `BBox` from the request envelope (WGS84).

        Returns:
            A `sentinelhub.BBox` over the request bbox.
        """
        sentinelhub = import_sentinelhub()
        return sentinelhub.BBox(
            (self.space.west, self.space.south, self.space.east, self.space.north),
            crs=sentinelhub.CRS.WGS84,
        )

    def _config(self) -> Any:
        """Return the authenticated `sentinelhub.SHConfig`.

        The auth wrapper is created in :meth:`_initialize` (run by the base
        constructor), so it is always present by the time any fetch method
        needs the SDK config.

        Returns:
            The `sentinelhub.SHConfig` from the configured auth wrapper.
        """
        assert self._auth is not None  # set in _initialize before any fetch
        return self._auth.config()

    def _request_size(self) -> tuple[int, int]:
        """Compute the render size in pixels via `bbox_to_dimensions`.

        Returns:
            `(width_px, height_px)` for the request bbox at `resolution`.
        """
        sentinelhub = import_sentinelhub()
        return cast(
            "tuple[int, int]",
            sentinelhub.bbox_to_dimensions(self._bbox(), resolution=self._resolution),
        )

    def _resolve_plane(self) -> str:
        """Resolve the request plane: explicit `api=`, else auto by size / geometry.

        Memoised: the plane is deterministic from instance state, so the result
        is computed once (one `bbox_to_dimensions` call) and reused across the
        `download` -> `_fetch` path.

        Returns:
            The resolved plane name.
        """
        if self._plane is None:
            has_geometry = self._geometry is not None
            has_s3 = self._batch_output is not None
            needs_size = self._api_mode is None or self._api_mode in RASTER_APIS
            max_side = max(self._request_size()) if needs_size else 0
            self._plane = resolve_api(self._api_mode, max_side, has_geometry, has_s3)
        return self._plane

    def _time_interval(self) -> tuple[str, str]:
        """Return the render time interval as ISO `(start, end)` date strings.

        Honours a per-window override set by the `aggregate=` render loop (C10);
        otherwise the full request window.
        """
        if self._window_override is not None:
            return self._window_override
        return (
            self.time.start_date.strftime("%Y-%m-%d"),
            self.time.end_date.strftime("%Y-%m-%d"),
        )

    def _search(self) -> list[RemoteProduct]:
        """List the planned requests without rendering (cheap dry-run).

        Returns one :class:`RemoteProduct` per requested key; the resolved row
        rides on `metadata["resolved"]`. No network call.

        Returns:
            One product per requested collection/recipe key.
        """
        return [
            RemoteProduct(id=key, metadata={"resolved": resolved})
            for key, resolved in self._resolved.items()
        ]

    def search(self, limit: int = 100) -> list[RemoteProduct]:
        """Query the Sentinel Hub Catalog API for scenes intersecting the request.

        A real STAC-style search (distinct from the internal :meth:`_search`
        fetch-planner): returns one :class:`RemoteProduct` per catalog item that
        intersects the request bbox + window, so a caller can enumerate coverage
        before rendering. An empty result is an empty list (not an error).

        Args:
            limit: Maximum number of items to return per requested collection.

        Returns:
            One product per catalog item (id + datetime + geometry on metadata).
        """
        sentinelhub = import_sentinelhub()
        cfg = self._config()
        catalog = sentinelhub.SentinelHubCatalog(config=cfg)
        sh_bbox = self._bbox()
        time_interval = self._time_interval()
        products: list[RemoteProduct] = []
        seen_collections: set[str] = set()
        for key, resolved in self._resolved.items():
            if resolved.sh_collection in seen_collections:
                continue
            seen_collections.add(resolved.sh_collection)
            collection = cdse_collection(resolved.sh_collection, cfg.sh_base_url)
            for item in catalog.search(
                collection, bbox=sh_bbox, time=time_interval, limit=limit
            ):
                properties = item.get("properties", {})
                products.append(
                    RemoteProduct(
                        id=item.get("id"),
                        metadata={
                            "key": key,
                            "collection": resolved.sh_collection,
                            "datetime": properties.get("datetime"),
                            "geometry": item.get("geometry"),
                        },
                    )
                )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Dispatch to the per-plane fetcher for the resolved `api`.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written raster paths (raster planes) or table paths / S3 URIs
            (tabular / batch planes).
        """
        plane = self._resolve_plane()
        fetchers = {
            "process": self._fetch_process,
            "async": self._fetch_async,
            "tiling": self._fetch_tiling,
            "batch": self._fetch_batch,
            "statistical": self._fetch_statistical,
            "batch-statistical": self._fetch_batch_statistical,
        }
        return cast("list[Any]", fetchers[plane](products))

    def _read_evalscript(self, resolved: ResolvedRequest) -> str:
        """Resolve the evalscript source for one request row.

        A custom `evalscript=` (an inline V3 JS string or a `.js` file path)
        wins over the recipe. Otherwise the recipe's bundled `.js` is read; a
        plain collection (no recipe evalscript and no custom one) is an error.

        Args:
            resolved: The resolved request row.

        Returns:
            The evalscript source string.

        Raises:
            ValueError: When the key is a plain collection and no `evalscript=`
                was supplied.
        """
        if self._evalscript is not None:
            candidate = self._evalscript
            try:
                path = Path(candidate)
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:
                pass
            return candidate
        if resolved.evalscript is None:
            raise ValueError(
                f"{resolved.key!r} is a plain collection, so it has no bundled "
                "evalscript. Pass evalscript= (an inline V3 JS string or a .js "
                "path), or request an evalscript recipe key instead."
            )
        return read_evalscript(resolved.evalscript)

    def _guard_process_size(self, size: tuple[int, int]) -> None:
        """Reject a Process request whose render exceeds the 2500 px cap.

        Args:
            size: The `(width, height)` render size in pixels.

        Raises:
            ValueError: When either side exceeds :data:`SH_MAX_DIMENSION`.
        """
        if max(size) > SH_MAX_DIMENSION:
            raise ValueError(
                f"the request renders to {size} px but the Process API caps a "
                f"single request at {SH_MAX_DIMENSION} px/side. Omit api= to "
                "auto-route to async / batch, or lower the resolution."
            )

    def _build_input_data(self, sentinelhub: Any, resolved: ResolvedRequest) -> dict:
        """Build the `SentinelHubRequest.input_data` block for one row.

        Args:
            sentinelhub: The imported `sentinelhub` module.
            resolved: The resolved request row.

        Returns:
            The `input_data` dict (collection, window, mosaicking order, maxcc).
        """
        cfg = self._config()
        collection = cdse_collection(resolved.sh_collection, cfg.sh_base_url)
        kwargs: dict[str, Any] = {
            "data_collection": collection,
            "time_interval": self._time_interval(),
            "mosaicking_order": self._mosaicking_order,
        }
        if self._maxcc is not None:
            kwargs["maxcc"] = self._maxcc
        return cast(
            "dict[Any, Any]", sentinelhub.SentinelHubRequest.input_data(**kwargs)
        )

    def _fetch_process(self, products: list[RemoteProduct]) -> list[Path]:
        """Render each product synchronously via the Process API → GeoTIFF on disk.

        Builds one `SentinelHubRequest` per resolved row over the request bbox +
        window at `resolution`, writes the rendered GeoTIFF under `path`, and
        returns the written file paths (resolved from the SDK rather than
        re-hashing).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written GeoTIFF paths, one per product.

        Raises:
            ValueError: When the render exceeds the Process 2500 px cap.
        """
        sentinelhub = import_sentinelhub()
        sh_bbox = self._bbox()
        size = self._request_size()
        self._guard_process_size(size)
        out: list[Path] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            out.append(
                self._render_process_tile(
                    sentinelhub, resolved, sh_bbox, size, str(self.root_dir)
                )
            )
        return out

    def _render_process_tile(
        self,
        sentinelhub: Any,
        resolved: ResolvedRequest,
        bbox: Any,
        size: tuple[int, int],
        data_folder: str,
    ) -> Path:
        """Build + run one Process request and return the written GeoTIFF path.

        Shared by :meth:`_fetch_process` (whole bbox) and :meth:`_fetch_tiling`
        (per tile).

        Args:
            sentinelhub: The imported `sentinelhub` module.
            resolved: The resolved request row.
            bbox: The `sentinelhub.BBox` to render.
            size: The `(width, height)` render size for this bbox.
            data_folder: The directory the SDK writes the GeoTIFF under.

        Returns:
            The written GeoTIFF path.
        """
        request = sentinelhub.SentinelHubRequest(
            evalscript=self._read_evalscript(resolved),
            input_data=[self._build_input_data(sentinelhub, resolved)],
            responses=[
                sentinelhub.SentinelHubRequest.output_response(
                    "default", sentinelhub.MimeType.TIFF
                )
            ],
            bbox=bbox,
            size=size,
            data_folder=data_folder,
            config=self._config(),
        )
        request.get_data(save_data=True)
        return cast("Path", Path(data_folder) / request.get_filename_list()[0])

    def _require_s3_delivery(self, sentinelhub: Any) -> Any:
        """Build the S3 `AccessSpecification` from `batch_output`, or error.

        Args:
            sentinelhub: The imported `sentinelhub` module.

        Returns:
            The S3 delivery `AccessSpecification`.

        Raises:
            ValueError: When no `batch_output` was supplied, or it carries no
                `bucket` / `url`.
        """
        if not self._batch_output:
            raise ValueError(
                "the async / batch planes deliver server-side to S3, so they "
                "need batch_output={'bucket': 's3://…', 'iam_role_arn': '…'}. "
                "Omit api= (or pass api='tiling') for a no-S3 oversized render."
            )
        spec = dict(self._batch_output)
        url = spec.pop("bucket", None) or spec.pop("url", None)
        if not url:
            raise ValueError(
                "batch_output must include a 'bucket' (or 'url') S3 destination, "
                f"got {dict(self._batch_output)!r}."
            )
        return sentinelhub.AsyncProcessRequest.s3_specification(url=url, **spec)

    def _guard_async_size(self, size: tuple[int, int]) -> None:
        """Reject an Async request beyond the 10000 px ceiling.

        Args:
            size: The `(width, height)` render size in pixels.

        Raises:
            ValueError: When either side exceeds :data:`ASYNC_MAX_DIMENSION`.
        """
        if max(size) > ASYNC_MAX_DIMENSION:
            raise ValueError(
                f"the request renders to {size} px but the Async Processing API "
                f"caps a request at {ASYNC_MAX_DIMENSION} px/side. Use api='batch' "
                "for a larger AOI."
            )

    def _fetch_async(self, products: list[RemoteProduct]) -> list[str]:
        """Render via the Async Processing API (S3-delivered, ≤10000 px).

        Submits one `AsyncProcessRequest` per row delivering to the `batch_output`
        S3 bucket, polls `get_async_running_status` to completion, and returns the
        S3 delivery URIs. Requires an S3 `batch_output` (the SDK's async plane is
        not a direct synchronous download).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The S3 **delivery prefix** (the configured `batch_output` bucket), one
            per product — not the individual written object keys (enumerate the
            bucket for those; earthlens does not perform S3 listing).

        Raises:
            ValueError: When no `batch_output` is set or the render exceeds the
                10000 px Async ceiling.
        """
        sentinelhub = import_sentinelhub()
        cfg = self._config()
        delivery = self._require_s3_delivery(sentinelhub)
        assert self._batch_output is not None  # _require_s3_delivery raises when unset
        bucket_uri = self._batch_output.get("bucket") or self._batch_output.get("url")
        sh_bbox = self._bbox()
        size = self._request_size()
        self._guard_async_size(size)
        out: list[str] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            evalscript = self._read_evalscript(resolved)
            request = sentinelhub.AsyncProcessRequest(
                evalscript=evalscript,
                input_data=[self._build_input_data(sentinelhub, resolved)],
                responses=[
                    sentinelhub.AsyncProcessRequest.output_response(
                        "default", sentinelhub.MimeType.TIFF
                    )
                ],
                delivery=delivery,
                bbox=sh_bbox,
                size=size,
                config=cfg,
            )
            # AsyncProcessRequest.get_data submits the job and returns the
            # submission JSON, which carries the async request id; poll on that
            # id (NOT the delivery URL — get_async_running_status resolves
            # `…/async/process/{id}`).
            request_id = _async_request_id(request.get_data(save_data=False))
            if request_id is not None:
                _wait_for_async(sentinelhub, [request_id], cfg)
            else:
                logger.warning(
                    "Sentinel Hub async: could not determine the request id from "
                    "the submission response; skipping the completion poll."
                )
            out.append(str(bucket_uri))
        logger.info(f"Sentinel Hub async: delivered {len(out)} object(s) to S3")
        return out

    def _fetch_tiling(self, products: list[RemoteProduct]) -> list[Path]:
        """Render an oversized AOI by local tiling + mosaic (no S3 needed).

        Splits the request bbox into ≤2500 px Process tiles, renders each tile,
        and mosaics them into one GeoTIFF per product with
        `pyramids.dataset.merge.merge_rasters`. Tile temporaries are written
        under a per-product subdirectory and removed after the merge.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The merged GeoTIFF paths, one per product (union bounds).

        Raises:
            ValueError: When a tile still renders above the Process cap (a
                rounding edge case); lower the `resolution` to re-tile finer.
        """
        import shutil

        from pyramids.dataset.merge import merge_rasters

        sentinelhub = import_sentinelhub()
        width, height = self._request_size()
        tiles = tile_bbox(
            (self.space.west, self.space.south, self.space.east, self.space.north),
            width,
            height,
        )
        # Pre-flight: size every tile once and guard against the Process cap
        # before rendering any, so a rounding edge case fails fast with a clear
        # error rather than a mid-run server 500 (and no partial tiles leak).
        tile_specs: list[tuple[Any, tuple[int, int]]] = []
        for tile in tiles:
            sh_bbox = sentinelhub.BBox(tile, crs=sentinelhub.CRS.WGS84)
            tile_size = sentinelhub.bbox_to_dimensions(
                sh_bbox, resolution=self._resolution
            )
            if max(tile_size) > SH_MAX_DIMENSION:
                raise ValueError(
                    f"a local tile renders to {tile_size} px, exceeding the "
                    f"{SH_MAX_DIMENSION} px Process cap; lower the resolution to "
                    "re-tile finer."
                )
            tile_specs.append((sh_bbox, tile_size))
        out: list[Path] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            tile_dir = Path(self.root_dir) / f"_tiles_{safe_filename(product.id)}"
            tile_dir.mkdir(parents=True, exist_ok=True)
            tile_paths: list[str] = []
            for index, (sh_bbox, tile_size) in enumerate(tile_specs):
                rendered = self._render_process_tile(
                    sentinelhub,
                    resolved,
                    sh_bbox,
                    tile_size,
                    str(tile_dir / str(index)),
                )
                tile_paths.append(str(rendered))
            merged = Path(self.root_dir) / f"{safe_filename(product.id)}.tif"
            merge_rasters(tile_paths, str(merged))
            shutil.rmtree(tile_dir, ignore_errors=True)
            out.append(merged)
        logger.info(f"Sentinel Hub tiling: merged {len(tiles)} tile(s) per product")
        return out

    def _fetch_batch(self, products: list[RemoteProduct]) -> list[str]:
        """Render a very large AOI via the Batch Processing API, tiled to S3.

        Builds the base Process request, then creates a batch request with a
        server-side tiling grid delivering to the `batch_output` S3 bucket,
        runs analysis → start → monitor, and returns the S3 destination URIs.
        Requires an S3 `batch_output` with at least a bucket; `grid_id` selects
        the tiling grid (default 0).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The S3 **delivery prefix** (the configured `batch_output` bucket), one
            per product — the server tiles the output under that prefix; enumerate
            the bucket for the individual tile object keys (earthlens does not
            perform S3 listing).

        Raises:
            ValueError: When no `batch_output` (S3 bucket) was supplied, or the
                analysed `cost_PU` exceeds `batch_output['max_cost_pu']`.
        """
        sentinelhub = import_sentinelhub()
        if not self._batch_output:
            raise ValueError(
                "the Batch Processing plane tiles server-side to S3, so it needs "
                "batch_output={'bucket': 's3://…', 'iam_role_arn': '…', "
                "'grid_id': <int>}."
            )
        cfg = self._config()
        client = sentinelhub.BatchProcessClient(config=cfg)
        spec = dict(self._batch_output)
        grid_id = spec.pop("grid_id", 0)
        buffer_x = spec.pop("buffer_x", None)
        buffer_y = spec.pop("buffer_y", None)
        max_cost_pu = spec.pop("max_cost_pu", None)
        url = spec.pop("bucket", None) or spec.pop("url", None)
        if not url:
            raise ValueError(
                "batch_output must include a 'bucket' (or 'url') S3 destination, "
                f"got {dict(self._batch_output)!r}."
            )
        delivery = client.s3_specification(url=url, **spec)
        tiling = client.tiling_grid_input(
            grid_id=grid_id,
            resolution=self._resolution,
            buffer_x=buffer_x,
            buffer_y=buffer_y,
        )
        output = client.raster_output(delivery=delivery)
        sh_bbox = self._bbox()
        out: list[str] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            base = sentinelhub.SentinelHubRequest(
                evalscript=self._read_evalscript(resolved),
                input_data=[self._build_input_data(sentinelhub, resolved)],
                responses=[
                    sentinelhub.SentinelHubRequest.output_response(
                        "default", sentinelhub.MimeType.TIFF
                    )
                ],
                bbox=sh_bbox,
                config=cfg,
            )
            batch_request = client.create(base, input=tiling, output=output)
            # Analyse first so the tile count / cost is known before committing
            # the (potentially continental, costly) job. start_analysis only
            # *starts* the analysis phase, so wait for it to finish before
            # reading cost_PU (otherwise the guard below sees None and is a no-op).
            client.start_analysis(batch_request)
            sentinelhub.monitor_batch_process_analysis(batch_request, client)
            batch_request = client.get_request(batch_request)
            cost_pu = getattr(batch_request, "cost_PU", None)
            logger.info(
                f"Sentinel Hub batch: analysed {product.id!r} (cost_PU={cost_pu})"
            )
            if (
                max_cost_pu is not None
                and cost_pu is not None
                and cost_pu > max_cost_pu
            ):
                raise ValueError(
                    f"batch analysis estimates cost_PU={cost_pu} for {product.id!r}, "
                    f"which exceeds batch_output['max_cost_pu']={max_cost_pu}; raise "
                    "the limit or shrink the request/resolution."
                )
            client.start_job(batch_request)
            sentinelhub.monitor_batch_process_job(batch_request, client)
            out.append(str(url))
        logger.info(f"Sentinel Hub batch: {len(out)} job(s) delivered to S3")
        return out

    def _statistical_evalscript(self, resolved: ResolvedRequest) -> str:
        """Resolve the evalscript for a Statistical request (must emit `dataMask`).

        Args:
            resolved: The resolved request row.

        Returns:
            The evalscript source.

        Raises:
            ValueError: When the evalscript does not declare a `dataMask` band
                (the Statistical API needs it to exclude invalid pixels).
        """
        script = self._read_evalscript(resolved)
        if "dataMask" not in script:
            raise ValueError(
                f"the Statistical API requires the evalscript to emit a "
                f"'dataMask' output band; {resolved.key!r} does not. Use a "
                "stats recipe (kind='stats', e.g. '…-ndvi-stats') or add a "
                "dataMask band to your custom evalscript."
            )
        return script

    def _statistical_interval(self) -> str:
        """The Statistical `aggregation_interval`: from `aggregate=`, else `P1D`."""
        if self._aggregate is not None:
            return interval_for(self._aggregate.freq)
        return "P1D"

    def _fetch_statistical(self, products: list[RemoteProduct]) -> list[Path]:
        """Compute zonal statistics via the Statistical API → a tidy table.

        Builds one `SentinelHubStatistical` request per geometry per product,
        flattens the nested interval→output→band→stats tree into rows, and writes
        one CSV per product (`feature_id` carried for a `FeatureCollection`).

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written table paths, one per product.

        Raises:
            ValueError: When no `geometry=` was supplied, or the evalscript lacks
                a `dataMask` band.
        """
        sentinelhub = import_sentinelhub()
        if self._geometry is None:
            raise ValueError(
                "the Statistical API computes zonal stats over a polygon, so it "
                "needs geometry= (a shapely geometry, a GeoJSON mapping, or a "
                "FeatureCollection)."
            )
        cfg = self._config()
        interval = self._statistical_interval()
        geometries = _iter_geometries(self._geometry)
        out: list[Path] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            evalscript = self._statistical_evalscript(resolved)
            collection = cdse_collection(resolved.sh_collection, cfg.sh_base_url)
            rows: list[dict] = []
            for feature_id, geom in geometries:
                # Size the sampling grid in pixels from the geometry's WGS84
                # bounds via bbox_to_dimensions (which converts metres -> px with
                # latitude). Passing `resolution=` here would be read in the
                # geometry CRS units (degrees), so a metre value collapses to a
                # single pixel and the server rejects the effective resolution.
                geom_bbox = sentinelhub.BBox(
                    _geometry_bounds(geom), crs=sentinelhub.CRS.WGS84
                )
                size = sentinelhub.bbox_to_dimensions(
                    geom_bbox, resolution=self._resolution
                )
                aggregation = sentinelhub.SentinelHubStatistical.aggregation(
                    evalscript=evalscript,
                    time_interval=self._time_interval(),
                    aggregation_interval=interval,
                    size=size,
                )
                input_kwargs: dict[str, Any] = {}
                if self._maxcc is not None:
                    input_kwargs["maxcc"] = self._maxcc
                request = sentinelhub.SentinelHubStatistical(
                    aggregation=aggregation,
                    input_data=[
                        sentinelhub.SentinelHubStatistical.input_data(
                            collection, **input_kwargs
                        )
                    ],
                    geometry=sentinelhub.Geometry(geom, crs=sentinelhub.CRS.WGS84),
                    calculations=_STAT_CALCULATIONS,
                    config=cfg,
                )
                payload = request.get_data()[0]
                rows.extend(_flatten_statistics(payload, feature_id=feature_id))
            if not rows:
                logger.warning(
                    f"Sentinel Hub statistical: no data returned for "
                    f"{product.id!r} over the request geometry + window "
                    "(empty table written)."
                )
            target = Path(self.root_dir) / f"{safe_filename(product.id)}.csv"
            _stats_frame(rows).to_csv(target, index=False)
            out.append(target)
        logger.info(f"Sentinel Hub statistical: wrote {len(out)} table(s)")
        return out

    def _fetch_batch_statistical(self, products: list[RemoteProduct]) -> list[Path]:
        """Compute zonal stats over a huge `FeatureCollection` via Batch Statistical.

        Submits one async batch-statistical job per product against the features
        uploaded to S3 (`batch_output['input_features']`), monitors it, retrieves
        the per-feature JSON from S3 via `AwsBatchStatisticalResults`, flattens it
        (reusing the C7 flattener, keyed by `feature_id`), and writes one CSV per
        product. Requires `batch_output` with an `input_features` S3 GeoPackage
        and an output bucket.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written table paths, one per product.

        Raises:
            ValueError: When `batch_output` is missing its `input_features` or
                output bucket, or the evalscript lacks a `dataMask` band.
        """
        from sentinelhub.aws import AwsBatchStatisticalResults

        sentinelhub = import_sentinelhub()
        if not self._batch_output:
            raise ValueError(
                "the Batch Statistical plane runs over a FeatureCollection on S3, "
                "so it needs batch_output={'input_features': 's3://…features.gpkg', "
                "'bucket': 's3://…out', 'iam_role_arn': '…'}."
            )
        spec = dict(self._batch_output)
        features_url = spec.pop("input_features", None)
        output_url = spec.pop("bucket", None) or spec.pop("output", None)
        feature_ids = spec.pop("feature_ids", None)
        if not features_url or not output_url:
            raise ValueError(
                "batch-statistical needs batch_output['input_features'] (the S3 "
                "GeoPackage of features) and an output bucket."
            )
        cfg = self._config()
        interval = self._statistical_interval()
        client = sentinelhub.SentinelHubBatchStatistical(config=cfg)
        out: list[Path] = []
        for product in products:
            resolved: ResolvedRequest = product.metadata["resolved"]
            evalscript = self._statistical_evalscript(resolved)
            aggregation = sentinelhub.SentinelHubStatistical.aggregation(
                evalscript=evalscript,
                time_interval=self._time_interval(),
                aggregation_interval=interval,
                resolution=(self._resolution, self._resolution),
            )
            input_kwargs: dict[str, Any] = {}
            if self._maxcc is not None:
                input_kwargs["maxcc"] = self._maxcc
            batch_request = client.create(
                input_features=client.s3_specification(url=features_url, **spec),
                input_data=[
                    sentinelhub.SentinelHubStatistical.input_data(
                        cdse_collection(resolved.sh_collection, cfg.sh_base_url),
                        **input_kwargs,
                    )
                ],
                aggregation=aggregation,
                calculations=_STAT_CALCULATIONS,
                output=client.s3_specification(url=output_url, **spec),
            )
            client.start_analysis(batch_request)
            client.start_job(batch_request)
            sentinelhub.monitor_batch_statistical_job(batch_request, cfg)
            results = AwsBatchStatisticalResults(
                batch_request,
                feature_ids=feature_ids,
                data_folder=str(self.root_dir),
                config=cfg,
            )
            payloads = results.get_data(save_data=True)
            rows: list[dict] = []
            ids = feature_ids if feature_ids is not None else range(len(payloads))
            for feature_id, payload in zip(ids, payloads):
                rows.extend(_flatten_statistics(payload, feature_id=feature_id))
            target = Path(self.root_dir) / f"{safe_filename(product.id)}.csv"
            _stats_frame(rows).to_csv(target, index=False)
            out.append(target)
        logger.info(f"Sentinel Hub batch-statistical: wrote {len(out)} table(s)")
        return out

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Any]:
        """Build the request(s), render server-side, and collect the output.

        Args:
            progress_bar: Reserved for parity with the other backends.
            aggregate: Optional aggregation request. Accepted (not rejected)
                because `OUTPUT_KIND` is `"mixed"`; applied per plane in C10.

        Returns:
            The written raster paths / table paths / S3 URIs, depending on the
            resolved plane.
        """
        self._aggregate = aggregate
        plane = self._resolve_plane()
        if aggregate is not None and plane in RASTER_APIS:
            results = self._aggregate_windows(aggregate)
        else:
            # Tabular planes apply aggregate via the Statistical
            # aggregation_interval (see _statistical_interval), so no loop here.
            results = self._api_via_search_fetch()
        logger.info(
            f"Sentinel Hub download: {len(results)} result(s) written to "
            f"{self.root_dir}"
        )
        return results

    def _aggregate_windows(self, aggregate: AggregationConfig) -> list[Any]:
        """Render one output per `aggregate.freq` window over the request span.

        Splits `[start, end]` into `freq` windows and renders the resolved raster
        plane once per window, stamping each local output
        `{key}_{freq}_{YYYYMMDD}.{suffix}` (the `ecmwf` / `cmems` per-window
        shape). S3-delivered planes (async / batch) return their per-window URIs
        unchanged.

        Args:
            aggregate: The aggregation request (its `freq` drives the windows).

        Returns:
            The per-window outputs across every requested key.
        """
        import pandas as pd

        edges = list(
            date_windows(self.time.start_date, self.time.end_date, aggregate.freq)
        )
        if not edges or pd.Timestamp(edges[0]) > pd.Timestamp(self.time.start_date):
            edges.insert(0, pd.Timestamp(self.time.start_date))
        results: list[Any] = []
        keys = list(self._resolved)
        try:
            for index, window_start in enumerate(edges):
                if index + 1 < len(edges):
                    # End the day before the next window starts so adjacent
                    # windows don't both claim the shared boundary date (the SH
                    # time_interval is inclusive on both ends).
                    window_end = pd.Timestamp(edges[index + 1]) - pd.Timedelta(days=1)
                    if window_end < pd.Timestamp(window_start):
                        window_end = pd.Timestamp(window_start)
                else:
                    window_end = pd.Timestamp(self.time.end_date)
                self._window_override = (
                    pd.Timestamp(window_start).strftime("%Y-%m-%d"),
                    pd.Timestamp(window_end).strftime("%Y-%m-%d"),
                )
                stamp = pd.Timestamp(window_start).strftime("%Y%m%d")
                produced = self._api_via_search_fetch()
                for key, item in zip(keys, produced):
                    results.append(
                        self._stamp_window_output(key, item, aggregate, stamp)
                    )
        finally:
            self._window_override = None
        return results

    def _stamp_window_output(
        self, key: str, item: Any, aggregate: AggregationConfig, stamp: str
    ) -> Any:
        """Rename a local per-window raster to the stamped name; pass URIs through.

        Args:
            key: The requested collection/recipe key.
            item: A produced output (a local `Path`/str, or an S3 URI string).
            aggregate: The aggregation request (its `freq` labels the file).
            stamp: The window's `YYYYMMDD` start stamp.

        Returns:
            The renamed local path, or the original S3 URI string.
        """
        source = Path(str(item))
        if not source.exists():
            return item
        suffix = source.suffix or ".tif"
        target = (
            Path(self.root_dir)
            / f"{safe_filename(key)}_{aggregate.freq}_{stamp}{suffix}"
        )
        source.replace(target)
        return target


#: The Statistical `calculations` block requesting the 5/50/95 percentiles
#: alongside the default per-band stats (min/max/mean/stDev/sampleCount).
_STAT_CALCULATIONS: dict = {
    "default": {"statistics": {"default": {"percentiles": {"k": [5, 50, 95]}}}}
}

#: The column order of the flattened Statistical table. Used as the header when a
#: query returns no data (e.g. no scene over the polygon in the window), so the
#: written CSV is a valid empty table rather than an unparseable header-less file.
_STAT_COLUMNS: tuple[str, ...] = (
    "feature_id",
    "interval_from",
    "interval_to",
    "output",
    "band",
    "min",
    "max",
    "mean",
    "stDev",
    "sampleCount",
    "noDataCount",
    "p5",
    "p50",
    "p95",
)


def _stats_frame(rows: list[dict]) -> Any:
    """Build the Statistical DataFrame, with a header even when `rows` is empty.

    Args:
        rows: The flattened per-band stat rows (possibly empty).

    Returns:
        A `pandas.DataFrame` — header-only when no stats were returned, so the
        written CSV always parses.
    """
    import pandas as pd

    if not rows:
        return pd.DataFrame(columns=list(_STAT_COLUMNS))
    return pd.DataFrame(rows)


def _iter_geometries(geometry: Any) -> list[tuple[Any, Any]]:
    """Normalise a `geometry=` value to `(feature_id, geom)` pairs.

    Accepts a GeoJSON `FeatureCollection` / `Feature` / bare-geometry mapping, a
    shapely geometry, or a list of any of those. A `FeatureCollection` yields one
    pair per feature (carrying its `id` / `properties.id` / positional index);
    everything else yields a single pair keyed `0`.

    Args:
        geometry: The request `geometry=` value.

    Returns:
        The list of `(feature_id, geom)` pairs to issue Statistical requests for.

    Examples:
        - A FeatureCollection yields one pair per feature:
            ```python
            >>> from earthlens.sentinel_hub.backend import _iter_geometries
            >>> fc = {"type": "FeatureCollection", "features": [
            ...     {"type": "Feature", "id": "a", "geometry": {"type": "Point"}},
            ...     {"type": "Feature", "id": "b", "geometry": {"type": "Point"}}]}
            >>> [fid for fid, _ in _iter_geometries(fc)]
            ['a', 'b']

            ```
    """
    if isinstance(geometry, dict):
        kind = geometry.get("type")
        if kind == "FeatureCollection":
            pairs: list[tuple[Any, Any]] = []
            for index, feature in enumerate(geometry.get("features", [])):
                fid = feature.get("id")
                if fid is None:
                    fid = (feature.get("properties") or {}).get("id", index)
                pairs.append((fid, feature["geometry"]))
            return pairs
        if kind == "Feature":
            return [(geometry.get("id", 0), geometry["geometry"])]
        return [(0, geometry)]
    if isinstance(geometry, (list, tuple)):
        return [(index, geom) for index, geom in enumerate(geometry)]
    return [(0, geometry)]


def _geometry_bounds(geom: Any) -> tuple[float, float, float, float]:
    """Return the `(west, south, east, north)` bounds of a geometry.

    Accepts a shapely geometry (uses `.bounds`) or a GeoJSON geometry mapping
    (recursively gathers its coordinate pairs). Used to size the Statistical
    sampling grid in pixels.

    Args:
        geom: A shapely geometry or a GeoJSON geometry mapping.

    Returns:
        The `(west, south, east, north)` envelope.

    Raises:
        ValueError: When no coordinates can be extracted from a mapping.

    Examples:
        - A GeoJSON polygon's bounds:
            ```python
            >>> from earthlens.sentinel_hub.backend import _geometry_bounds
            >>> poly = {"type": "Polygon", "coordinates":
            ...     [[[14.0, 40.0], [14.2, 40.0], [14.2, 40.1], [14.0, 40.1]]]}
            >>> _geometry_bounds(poly)
            (14.0, 40.0, 14.2, 40.1)

            ```
    """
    bounds = getattr(geom, "bounds", None)
    if bounds is not None and not isinstance(geom, dict):
        west, south, east, north = bounds
        return (west, south, east, north)
    coordinates = geom.get("coordinates", geom) if isinstance(geom, dict) else geom
    points: list[tuple[float, float]] = []

    def _walk(node: Any) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            points.append((node[0], node[1]))
        elif isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(coordinates)
    if not points:
        raise ValueError("could not extract coordinates from the geometry.")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _flatten_statistics(payload: dict, feature_id: Any) -> list[dict]:
    """Flatten a Statistical `get_data()[0]` payload into tidy per-band rows.

    Walks the `data → interval → outputs → bands → stats/percentiles` tree,
    skipping the `dataMask` output, and emits one row per
    (interval × output × band) with the standard stats + 5/50/95 percentiles.

    Args:
        payload: One element of `SentinelHubStatistical.get_data()`.
        feature_id: The id to stamp on every row (for a `FeatureCollection`).

    Returns:
        The flattened rows.

    Examples:
        - One interval with one band yields one row:
            ```python
            >>> from earthlens.sentinel_hub.backend import _flatten_statistics
            >>> payload = {"data": [{"interval": {"from": "2020-06-01T00:00:00Z",
            ...     "to": "2020-06-02T00:00:00Z"}, "outputs": {"ndvi": {"bands":
            ...     {"B0": {"stats": {"mean": 0.4, "min": 0.1, "max": 0.7}}}}}}]}
            >>> rows = _flatten_statistics(payload, feature_id="farm-1")
            >>> rows[0]["mean"], rows[0]["feature_id"], rows[0]["band"]
            (0.4, 'farm-1', 'B0')

            ```
    """
    rows: list[dict] = []
    for entry in payload.get("data", []):
        interval = entry.get("interval", {})
        for output_name, output_body in (entry.get("outputs") or {}).items():
            if output_name == "dataMask":
                continue
            for band_name, band_body in (output_body.get("bands") or {}).items():
                stats = band_body.get("stats", {})
                row = {
                    "feature_id": feature_id,
                    "interval_from": interval.get("from"),
                    "interval_to": interval.get("to"),
                    "output": output_name,
                    "band": band_name,
                    "min": stats.get("min"),
                    "max": stats.get("max"),
                    "mean": stats.get("mean"),
                    "stDev": stats.get("stDev"),
                    "sampleCount": stats.get("sampleCount"),
                    "noDataCount": stats.get("noDataCount"),
                }
                for percentile, value in (band_body.get("percentiles") or {}).items():
                    row[f"p{percentile}"] = value
                rows.append(row)
    return rows


def _async_request_id(submission: Any) -> str | None:
    """Extract the async request id from an `AsyncProcessRequest.get_data` payload.

    The Async Process API submission returns a JSON document carrying the job
    `id`; `get_data` yields it wrapped in a list. This pulls the id out
    defensively (the only field the completion poll needs).

    Args:
        submission: The value returned by `AsyncProcessRequest.get_data`.

    Returns:
        The async request id, or `None` when it cannot be determined.

    Examples:
        - The id is read from the first response element:
            ```python
            >>> from earthlens.sentinel_hub.backend import _async_request_id
            >>> _async_request_id([{"id": "abc-123", "status": "CREATED"}])
            'abc-123'

            ```
        - An empty / unexpected payload yields `None`:
            ```python
            >>> from earthlens.sentinel_hub.backend import _async_request_id
            >>> _async_request_id([]) is None
            True

            ```
    """
    if not submission:
        return None
    first = submission[0] if isinstance(submission, (list, tuple)) else submission
    if isinstance(first, dict):
        # `or None` so an empty/absent id yields None (the caller then skips the
        # poll) rather than a falsy id that would poll a bogus `…/process/` URL.
        return first.get("id") or first.get("requestId") or None
    return None


#: Async-plane polling cadence (seconds) and the attempt ceiling (~1 hour).
_ASYNC_POLL_SECONDS = 10.0
_ASYNC_MAX_ATTEMPTS = 360


def _wait_for_async(
    sentinelhub: Any,
    ids: list,
    config: Any,
    poll_seconds: float = _ASYNC_POLL_SECONDS,
    max_attempts: int = _ASYNC_MAX_ATTEMPTS,
) -> None:
    """Poll `get_async_running_status` until no listed request is still running.

    Args:
        sentinelhub: The imported `sentinelhub` module.
        ids: The async request ids / urls to poll (falsy entries skipped).
        config: The `SHConfig` to authenticate the status calls.
        poll_seconds: Delay between status polls.
        max_attempts: Maximum number of polls before giving up.

    Raises:
        TimeoutError: When jobs are still running after `max_attempts` polls.
    """
    import time

    active = [item for item in ids if item]
    if not active:
        return
    for _ in range(max_attempts):
        status = sentinelhub.get_async_running_status(active, config)
        if not any(status.get(item, False) for item in active):
            return
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"async Sentinel Hub jobs still running after {max_attempts} polls: {active}"
    )
