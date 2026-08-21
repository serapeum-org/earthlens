"""openEO server-side-processing backend (gridded raster output).

`OpenEO(AbstractDataSource)` builds an [openEO](https://openeo.org) **process
graph** — a JSON DAG of `load_collection → (recipe steps) → aggregate → save` —
which the **backend executes server-side**; earthlens authenticates, builds the
graph from a curated recipe/collection catalog, triggers execution, and
downloads the gridded result (GeoTIFF / NetCDF) to `path`.

Its closest sibling is the GEE backend: the *server* does the compute (band
math, masking, temporal reduction, reprojection) and earthlens just orchestrates.
Unlike GEE, openEO is provider-agnostic and OSS, the graph is portable JSON, and
`aggregate=` is a **native** openEO process (`aggregate_temporal_period`) rather
than a pyramids post-step — so `aggregate=` costs no client compute here.

A request is `variables={collection_or_recipe: [band, ...]}` plus a bbox + date
window. A key may name a **collection** (default graph = load → clip → save) or a
**recipe** (a fixed, curated graph). It defaults to CDSE openEO
(`openeo.dataspace.copernicus.eu`, free with a CDSE account); `endpoint=` selects
the CDSE federation or openEO Platform.

`OUTPUT_KIND = "raster"`, so the `EarthLens` facade forwards
`aggregate=AggregationConfig(...)` to `download()` rather than rejecting it.
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
    safe_filename,
)
from earthlens.openeo._helpers import OUTPUT_FORMATS, period_for, reducer_for
from earthlens.openeo.auth import OpeneoAuth, OpeneoCredentials

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.openeo.catalog import Catalog, ResolvedGraph


class OpenEO(AbstractDataSource):
    """Server-side openEO backend (Planetary-agnostic; defaults to CDSE).

    Attributes:
        OUTPUT_KIND: `"raster"` — the server writes a GeoTIFF / NetCDF; the
            facade forwards `aggregate=` (translated to a server-side
            `aggregate_temporal_period` node, not a pyramids post-step).
    """

    OUTPUT_KIND: OutputKind = "raster"

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
        endpoint: str | None = None,
        process: str | None = None,
        execute: str = "sync",
        output_format: str = "GTiff",
        max_cloud_cover: float | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        provider_id: str | None = None,
    ):
        """Initialise an openEO backend instance.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            variables: `{collection_or_recipe_key: [band, ...]}`. A key is a
                catalog **collection** (default graph) or a **recipe** (fixed
                graph); an empty band list falls back to the collection /
                recipe default bands.
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory cadence label; the search window is
                `start`/`end`.
            path: Output directory (created by the parent class).
            fmt: `strptime` format for `start` / `end` (the date format — not
                the output raster format; see `output_format`).
            endpoint: Endpoint alias (`"cdse"`, `"cdse-federation"`,
                `"openeo-platform"`) or a full URL. Defaults to CDSE core.
            process: Optional explicit recipe key to apply, overriding the
                recipe/collection inferred from a `variables` key.
            execute: `"sync"` (default; `DataCube.download`, size-capped) or
                `"batch"` (a polled batch job for large AOIs / windows).
            output_format: openEO output format — `"GTiff"` (default) or
                `"netCDF"`. Named distinctly from `fmt` (the date format) to
                avoid colliding with the base/facade date argument.
            max_cloud_cover: Optional server-side `max_cloud_cover` property
                filter applied on `load_collection` (Sentinel-2).
            client_id: OIDC client id for the headless client-credentials flow
                (else `OPENEO_CLIENT_ID`).
            client_secret: OIDC client secret (else `OPENEO_CLIENT_SECRET`).
            refresh_token: OIDC refresh token for the refresh-token flow (else
                `OPENEO_REFRESH_TOKEN`).
            provider_id: Optional OIDC provider id (else `OPENEO_PROVIDER_ID`).

        Raises:
            ValueError: When `variables` is empty, `execute` is not
                `"sync"`/`"batch"`, or `output_format` is unknown.
        """
        if execute not in {"sync", "batch"}:
            raise ValueError(f"execute must be 'sync' or 'batch', got {execute!r}.")
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {sorted(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )
        self._endpoint = endpoint
        self._process = process
        self._execute = execute
        self._output_format = output_format
        self._max_cloud_cover = max_cloud_cover
        # Credentials: explicit kwargs win over the OPENEO_* environment.
        env_creds = OpeneoCredentials.from_env()
        self._credentials = OpeneoCredentials(
            client_id=client_id or env_creds.client_id,
            client_secret=(
                SecretStr(client_secret) if client_secret else env_creds.client_secret
            ),
            refresh_token=(
                SecretStr(refresh_token) if refresh_token else env_creds.refresh_token
            ),
            provider_id=provider_id or env_creds.provider_id,
        )
        # Stored before super().__init__ because the base constructor calls
        # _initialize() (which needs the request) before it sets self.vars.
        self._variables = variables
        self._catalog: Catalog | None = None
        self._auth: OpeneoAuth | None = None
        # Resolved {key: catalog row} populated in _initialize.
        self._resolved: dict[str, Any] = {}
        # Aggregation request captured by download(), translated to a graph node.
        self._aggregate: AggregationConfig | None = None
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
        wrapper is stored on `self._auth` and the resolved catalog rows on
        `self._resolved`. The OIDC flow is deferred to first use (lazy) so
        construction never blocks on the network.

        Raises:
            TypeError: When `variables` is not a
                `{collection_or_recipe: [band, ...]}` mapping (e.g. a bare
                `list`), so the misuse surfaces at construction instead of as a
                late `AttributeError` during the download.
            ValueError: When `variables` is empty or names a key the catalog
                does not know (with a did-you-mean hint).
        """
        from earthlens.openeo.catalog import Catalog

        if not isinstance(self._variables, dict):
            raise TypeError(
                "openEO requires variables as a "
                "{collection_or_recipe: [band, ...]} mapping, got "
                f"{type(self._variables).__name__}."
            )
        if not self._variables:
            raise ValueError(
                "openEO requires variables={collection_or_recipe: [band, ...]} "
                "with at least one collection or recipe key."
            )
        self._catalog = Catalog()
        self._resolved = {
            key: self._catalog.resolve(self._process or key) for key in self._variables
        }
        self._auth = OpeneoAuth(self._credentials, endpoint=self._endpoint)
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            temporal_resolution: Advisory cadence label (`"daily"`, `"monthly"`,
                `"hourly"`, `"yearly"`) — maps to the pandas frequency stored on
                the returned extent; the openEO window itself is `start`/`end`.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: The parsed window (with an inclusive `end_date`; the
            backend converts it to openEO's exclusive bound at graph-build time).

        Raises:
            ValueError: When `temporal_resolution` is not one of the accepted
                cadences. A missing `start` / `end` is rejected earlier, by
                `AbstractDataSource._check_time_window` — openEO needs an
                explicit `temporal_extent`, so it keeps the inherited
                `REQUIRES_TIME_WINDOW = True`.
        """
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

    def _search(self) -> list[RemoteProduct]:
        """List the planned graphs without executing anything (cheap dry-run).

        Returns one :class:`RemoteProduct` per requested key; the resolved
        collection/recipe row rides on `metadata["resolved"]`. No network call —
        inspecting the result is a "what would I build?" preview.

        Returns:
            One product per requested collection/recipe key.
        """
        return [
            RemoteProduct(id=key, metadata={"resolved": resolved})
            for key, resolved in self._resolved.items()
        ]

    def _request_bands(self, key: str, resolved: ResolvedGraph) -> list[str]:
        """Pick the bands to load: the request's override, else the row default.

        Args:
            key: The requested collection/recipe key.
            resolved: The resolved row for `key`.

        Returns:
            The bands to pass to `load_collection` (may be empty → all bands).
        """
        requested = self._variables.get(key) or []
        return list(requested) if requested else list(resolved.bands)

    def _build_cube(self, conn: Any, key: str, resolved: ResolvedGraph) -> Any:
        """Build the openEO `DataCube` for one requested key.

        Loads the collection over the request's bbox + window + bands, applies
        the recipe's graph steps (if any), and appends a server-side
        `aggregate_temporal_period` node when `aggregate=` was supplied.

        Args:
            conn: The authenticated openEO connection.
            key: The requested collection/recipe key.
            resolved: The resolved row for `key`.

        Returns:
            The fully-built `DataCube` ready to download / submit.

        Raises:
            ValueError: When `max_cloud_cover` was supplied but the collection
                does not expose `eo:cloud_cover` (non-optical collections), so
                the filter would be silently ignored or rejected server-side.
        """
        bands = self._request_bands(key, resolved)
        load_kwargs: dict[str, Any] = {}
        if self._max_cloud_cover is not None:
            if not resolved.supports_cloud_cover:
                raise ValueError(
                    f"max_cloud_cover is only supported for optical collections "
                    f"that expose eo:cloud_cover (Sentinel-2); {resolved.key!r} "
                    f"({resolved.collection_id}) does not. Drop max_cloud_cover "
                    "for this request."
                )
            load_kwargs["max_cloud_cover"] = self._max_cloud_cover
        cube = conn.load_collection(
            resolved.collection_id,
            spatial_extent={
                "west": self.space.west,
                "south": self.space.south,
                "east": self.space.east,
                "north": self.space.north,
            },
            temporal_extent=[
                self.time.start_date.strftime("%Y-%m-%d"),
                _exclusive_end(self.time.end_date),
            ],
            bands=(bands or None),
            **load_kwargs,
        )
        for step in resolved.graph:
            cube = _apply_step(cube, step)
        if self._aggregate is not None:
            cube = cube.aggregate_temporal_period(
                period=period_for(self._aggregate.freq),
                reducer=reducer_for(self._aggregate.op),
            )
        return cube

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Build, execute, and download one output file per product.

        For each product: build its `DataCube`, then either download it
        synchronously (`execute="sync"`, size-capped) or run a polled batch job
        (`execute="batch"`). The output format is the recipe's `output_format`
        when set, else the backend's `output_format`.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            The written file paths, one per product (request key order).
        """
        assert self._auth is not None  # set in _initialize before any download
        conn = self._auth.connection()
        out: list[Path] = []
        for product in products:
            resolved: ResolvedGraph = product.metadata["resolved"]
            cube = self._build_cube(conn, product.id, resolved)
            out_format = resolved.output_format or self._output_format
            suffix = OUTPUT_FORMATS[out_format]
            target = Path(self.root_dir) / f"{safe_filename(product.id)}.{suffix}"
            if self._execute == "batch":
                job = cube.create_job(out_format=out_format)
                job.start_and_wait().get_results().download_file(str(target))
            else:
                cube.download(str(target), format=out_format)
            out.append(target)
        return out

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Build the process graph(s), execute server-side, and download outputs.

        Args:
            progress_bar: Reserved for parity with the other backends.
            aggregate: Optional aggregation request. Accepted (not rejected)
                because `OUTPUT_KIND` is `"raster"`; translated into a
                server-side `aggregate_temporal_period` node in each graph.

        Returns:
            The downloaded file paths, one per requested key.
        """
        self._aggregate = aggregate
        paths = self._api_via_search_fetch()
        logger.info(f"openEO download: {len(paths)} file(s) written to {self.root_dir}")
        return paths


def _apply_step(cube: Any, step: dict[str, dict[str, Any]]) -> Any:
    """Apply one recipe graph step to a `DataCube`.

    A step is a single-key mapping `{process_name: {param: value, ...}}`. When
    `process_name` is a `DataCube` method (`ndvi`, `aggregate_temporal_period`,
    `reduce_dimension`, …) it is called directly; otherwise — for a
    backend-only process such as `mask_scl_dilation` or `sar_backscatter` — the
    generic `DataCube.process` is used with the cube bound as the `data`
    argument (`DataCube.process` does not auto-inject `data`).

    Args:
        cube: The current `DataCube`.
        step: The single-key `{process: params}` step.

    Returns:
        The new `DataCube` after the step.

    Examples:
        - Dispatch to a DataCube method:
            ```python
            >>> from earthlens.openeo.backend import _apply_step
            >>> class _Cube:
            ...     def ndvi(self, nir, red):
            ...         return f"ndvi({nir},{red})"
            >>> _apply_step(_Cube(), {"ndvi": {"nir": "B08", "red": "B04"}})
            'ndvi(B08,B04)'

            ```
        - Fall back to the generic process for a backend-only step:
            ```python
            >>> from earthlens.openeo.backend import _apply_step
            >>> class _Cube:
            ...     def process(self, name, arguments):
            ...         return (name, sorted(arguments))
            >>> _apply_step(_Cube(), {"mask_scl_dilation": {}})
            ('mask_scl_dilation', ['data'])

            ```
    """
    ((name, params),) = step.items()
    method = getattr(cube, name, None)
    if callable(method):
        return method(**params)
    return cube.process(name, arguments={"data": cube, **params})


def _exclusive_end(end_date: Any) -> str:
    """Convert an inclusive end date to openEO's exclusive (right-open) bound.

    earthlens presents the request `end` as **inclusive** (matching every other
    backend); openEO's `load_collection` `temporal_extent` is left-closed,
    right-open — it *excludes* its end instant. Advancing the bound by one day
    preserves the inclusive day (and keeps a single-day `start == end` request
    non-empty).

    Args:
        end_date: The inclusive end as a `datetime` (from `_check_input_dates`).

    Returns:
        The exclusive end as a `YYYY-MM-DD` string (`end_date + 1 day`).

    Examples:
        - The inclusive end is advanced by one day:
            ```python
            >>> import datetime as dt
            >>> from earthlens.openeo.backend import _exclusive_end
            >>> _exclusive_end(dt.datetime(2023, 6, 30))
            '2023-07-01'

            ```
        - A single-day window stays non-empty (`[d, d+1)`):
            ```python
            >>> import datetime as dt
            >>> from earthlens.openeo.backend import _exclusive_end
            >>> _exclusive_end(dt.datetime(2023, 6, 1))
            '2023-06-02'

            ```
    """
    import datetime as dt

    return cast("str", (end_date + dt.timedelta(days=1)).strftime("%Y-%m-%d"))
