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
from typing import TYPE_CHECKING, Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.sentinel_hub._dispatch import resolve_api, validate_api
from earthlens.sentinel_hub._helpers import RASTER_APIS, import_sentinelhub
from earthlens.sentinel_hub.auth import SentinelHubAuth, SentinelHubCredentials

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.sentinel_hub.catalog import Catalog, ResolvedRequest

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
            client_id: OAuth client id (else `SH_CLIENT_ID`).
            client_secret: OAuth client secret (else `SH_CLIENT_SECRET`).
            profile: A saved `SHConfig` profile name (else `SH_PROFILE`).

        Raises:
            ValueError: When `api` is an unknown plane, or `mosaicking_order` is
                not a recognised value.
        """
        validate_api(api)
        if mosaicking_order not in {"mostRecent", "leastRecent", "leastCC"}:
            raise ValueError(
                "mosaicking_order must be 'mostRecent', 'leastRecent', or "
                f"'leastCC', got {mosaicking_order!r}."
            )
        self._resolution = resolution
        self._evalscript = evalscript
        self._endpoint = endpoint
        self._mosaicking_order = mosaicking_order
        self._api = api
        self._geometry = geometry
        self._maxcc = maxcc
        self._batch_output = batch_output
        env_creds = SentinelHubCredentials.from_env()
        self._credentials = SentinelHubCredentials(
            client_id=client_id or env_creds.client_id,
            client_secret=client_secret or env_creds.client_secret,
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
        self._resolved = {
            key: self._catalog.resolve(key) for key in self._variables
        }
        self._auth = SentinelHubAuth(self._credentials, endpoint=self._endpoint)
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Build the WGS84 envelope the Sentinel Hub `BBox` is built from.

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
        """Parse the date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive start date string (parsed with `fmt`).
            end: Inclusive end date string.
            temporal_resolution: Advisory cadence label (`"daily"`, `"monthly"`,
                `"hourly"`, `"yearly"`) → the pandas frequency on the extent.
            fmt: `strptime` format for `start` / `end`.

        Returns:
            TemporalExtent: The parsed window (inclusive `end_date`).

        Raises:
            ValueError: When `start` or `end` is `None`.
        """
        import datetime as dt

        import pandas as pd

        if start is None or end is None:
            raise ValueError(
                "Sentinel Hub requires both start and end dates (the render "
                "needs a time_interval); pass start=… and end=…."
            )
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h", "yearly": "YS"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt, end_date=end_dt, resolution=resolution, dates=dates
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

    def _request_size(self) -> tuple[int, int]:
        """Compute the render size in pixels via `bbox_to_dimensions`.

        Returns:
            `(width_px, height_px)` for the request bbox at `resolution`.
        """
        sentinelhub = import_sentinelhub()
        return sentinelhub.bbox_to_dimensions(self._bbox(), resolution=self._resolution)

    def _resolve_plane(self) -> str:
        """Resolve the request plane: explicit `api=`, else auto by size / geometry.

        Returns:
            The resolved plane name.
        """
        has_geometry = self._geometry is not None
        needs_size = self._api is None or self._api in RASTER_APIS
        max_side = max(self._request_size()) if needs_size else 0
        return resolve_api(self._api, max_side, has_geometry)

    def _time_interval(self) -> tuple[str, str]:
        """Return the render time interval as ISO `(start, end)` date strings."""
        return (
            self.time.start_date.strftime("%Y-%m-%d"),
            self.time.end_date.strftime("%Y-%m-%d"),
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical search/fetch shape."""
        return self._api_via_search_fetch()

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
            "batch": self._fetch_batch,
            "statistical": self._fetch_statistical,
            "batch-statistical": self._fetch_batch_statistical,
        }
        return fetchers[plane](products)

    def _fetch_process(self, products: list[RemoteProduct]) -> list[Path]:
        """Render each product synchronously via the Process API (C3)."""
        raise NotImplementedError(
            "the Process render plane is implemented in C3."
        )

    def _fetch_async(self, products: list[RemoteProduct]) -> list[Path]:
        """Render via the Async Processing API (C5)."""
        raise NotImplementedError(
            "the Async Processing plane is implemented in C5."
        )

    def _fetch_batch(self, products: list[RemoteProduct]) -> list[Any]:
        """Render via the Batch Processing API to S3 (C8)."""
        raise NotImplementedError(
            "the Batch Processing plane is implemented in C8."
        )

    def _fetch_statistical(self, products: list[RemoteProduct]) -> list[Path]:
        """Compute zonal statistics via the Statistical API (C7)."""
        raise NotImplementedError(
            "the Statistical plane is implemented in C7."
        )

    def _fetch_batch_statistical(self, products: list[RemoteProduct]) -> list[Path]:
        """Compute zonal statistics via the Batch Statistical API (C9)."""
        raise NotImplementedError(
            "the Batch Statistical plane is implemented in C9."
        )

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
        results = self._api_via_search_fetch()
        logger.info(
            f"Sentinel Hub download: {len(results)} result(s) written to "
            f"{self.root_dir}"
        )
        return results
