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
from typing import TYPE_CHECKING, Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.openeo._helpers import OUTPUT_FORMATS
from earthlens.openeo.auth import OpeneoAuth, OpeneoCredentials

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.openeo.catalog import Catalog


class OpenEO(AbstractDataSource):
    """Server-side openEO backend (Planetary-agnostic; defaults to CDSE).

    Attributes:
        OUTPUT_KIND: `"raster"` — the server writes a GeoTIFF / NetCDF; the
            facade forwards `aggregate=` (translated to a server-side
            `aggregate_temporal_period` node, not a pyramids post-step).
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
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
            raise ValueError(
                f"execute must be 'sync' or 'batch', got {execute!r}."
            )
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
            client_secret=client_secret or env_creds.client_secret,
            refresh_token=refresh_token or env_creds.refresh_token,
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
            ValueError: When `variables` is empty or names a key the catalog
                does not know (with a did-you-mean hint).
        """
        from earthlens.openeo.catalog import Catalog

        if not self._variables:
            raise ValueError(
                "openEO requires variables={collection_or_recipe: [band, ...]} "
                "with at least one collection or recipe key."
            )
        self._catalog = Catalog()
        self._resolved = {
            key: self._catalog.resolve(self._process or key)
            for key in self._variables
        }
        self._auth = OpeneoAuth(self._credentials, endpoint=self._endpoint)
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Build the WGS84 envelope passed straight to openEO `spatial_extent`.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: The request's bounding box.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date window into a :class:`TemporalExtent`."""
        import datetime as dt

        import pandas as pd

        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h", "yearly": "YS"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt, end_date=end_dt, resolution=resolution, dates=dates
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical search/fetch shape."""
        return self._api_via_search_fetch()

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
        logger.info(
            f"openEO download: {len(paths)} file(s) written to {self.root_dir}"
        )
        return paths
