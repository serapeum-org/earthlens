"""Backend that queries Protected Planet (WDPA) protected areas via v4 REST.

`WDPA(AbstractDataSource)` fetches protected-area **polygons** from the
World Database on Protected and Conserved Areas (Protected Planet) — 300 k+
areas — through a thin direct v4 REST client (`earthlens.wdpa._rest`). The
`pywdpa` package is not used: it targets the retired v3 API (taken down
2026-05-01) and writes a shapefile to disk. v4 requires a personal token
passed as a `?token=` query parameter (resolved by :class:`WdpaAuth`).

This is the cluster's only polygon-geometry backend (`OUTPUT_KIND =
"vector"`). Each entry in `variables` selects a country (ISO3 code) or a
single WDPA id; the per-area GeoJSON geometry is assembled into a polygon
:class:`~pyramids.feature.collection.FeatureCollection`. `download()`
returns it and, when `path` is set, writes it (GeoParquet by default).

WDPA data carries a custom UNEP-WCMC license (commercial use needs written
permission, redistribution is restricted), so every fetch raises a
`LicenseWarning`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
from loguru import logger
from pyramids.feature.collection import FeatureCollection

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.biodiversity import WDPA_LICENSE, warn_license
from earthlens.wdpa import _rest
from earthlens.wdpa.auth import WdpaAuth, WdpaCredentials
from earthlens.wdpa.catalog import Catalog

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

FileFormat = Literal["geoparquet", "gpkg", "geojson"]

#: Output format -> (OGR driver or `"parquet"`, file extension).
_FORMATS: dict[str, tuple[str, str]] = {
    "geoparquet": ("parquet", "parquet"),
    "gpkg": ("GPKG", "gpkg"),
    "geojson": ("GeoJSON", "geojson"),
}


class WDPA(AbstractDataSource):
    """Protected Planet (WDPA) backend (vector protected-area polygons).

    Wraps the Protected Planet v4 REST API so a user can pull a country's
    (or one id's) protected-area polygons through the same `download()`
    shape every other earthlens backend uses. Each entry in `variables`
    is a country ISO3 code (`"KEN"` or `"country:KEN"`) or a WDPA id;
    results across entries are unioned into one polygon
    :class:`~pyramids.feature.collection.FeatureCollection`.

    Attributes:
        OUTPUT_KIND: `"vector"` — the facade rejects `aggregate=`.
    """

    OUTPUT_KIND: OutputKind = "vector"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list,
        lat_lim: list,
        lon_lim: list,
        temporal_resolution: str = "all",
        path: str = "",
        fmt: str = "%Y-%m-%d",
        token: str | None = None,
        file_format: FileFormat = "geoparquet",
    ):
        """Configure a WDPA protected-area query.

        Args:
            start: Inclusive start date string (parsed with `fmt`); WDPA
                is not time-filtered, so this only frames the request.
            end: Inclusive end date string (parsed with `fmt`).
            variables: Country / area selectors — ISO3 country codes
                (`["KEN"]` or `["country:KEN"]`) or WDPA ids (`["555"]`).
                Must be non-empty.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: WDPA is not time-chunked, so this is the
                sentinel `"all"`.
            path: Output directory for the protected-area file. The empty
                string (the default) opts out of writing — `download()`
                returns the in-memory FeatureCollection without touching
                the filesystem. Pass an explicit directory to write the file.
            fmt: `strptime` format for `start` / `end`.
            token: Protected Planet API token; falls back to the
                `WDPA_TOKEN` environment variable.
            file_format: Output vector format — `"geoparquet"` (default),
                `"gpkg"`, or `"geojson"`.

        Raises:
            TypeError: If `variables` is a mapping.
            ValueError: If `variables` is empty or `file_format` unknown.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "WDPA `variables` must be a list of country/area selectors "
                "(e.g. ['KEN'] or ['country:KEN']), not a mapping."
            )
        if not list(variables):
            raise ValueError(
                "WDPA `variables` must name at least one country (ISO3) or "
                "WDPA id (e.g. ['KEN'])."
            )
        if file_format not in _FORMATS:
            raise ValueError(
                f"file_format must be one of {sorted(_FORMATS)}, got {file_format!r}."
            )
        self._file_format: FileFormat = file_format
        self._token_arg = token
        self._auth: WdpaAuth | None = None
        # Preserve the user's original `path` so download() can honour
        # `path=""` as "do not write a file".
        self._user_path = path
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Build and configure the token auth (surfaces a missing token early).

        Returns:
            None: No long-lived client; `_rest` builds a session per call.

        Raises:
            AuthenticationError: When no `WDPA_TOKEN` / `token=` is set.
        """
        self._auth = WdpaAuth(WdpaCredentials(token=self._token_arg))
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse `[start, end]` into a :class:`TemporalExtent`.

        WDPA is not time-filtered, so the window only frames the request
        and the resolution is the sentinel `"all"`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: The sentinel `"all"`.
            fmt: `strptime` format for both ends.

        Returns:
            TemporalExtent: The validated `[start, end]` window.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="all",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _fetch(self):
        """Fetch every selector's protected areas as one GeoDataFrame.

        Routes each `variables` entry to the country or by-id v4 fetch,
        concatenates the polygon GeoDataFrames, and returns the union.

        Returns:
            gpd.GeoDataFrame: The protected-area polygons, CRS `EPSG:4326`.
        """
        token = self._auth.token
        frames = [self._fetch_one(token, selector) for selector in self.vars]
        non_empty = [frame for frame in frames if len(frame)]
        if not non_empty:
            return frames[0]
        import geopandas as gpd

        merged = pd.concat(non_empty, ignore_index=True)
        return gpd.GeoDataFrame(merged, geometry="geometry", crs=_rest.CRS)

    def _fetch_one(self, token: str, selector: str):
        """Fetch one selector (country or WDPA id).

        A numeric selector is a WDPA id; anything else is resolved to an
        ISO3 country code through the catalog (which accepts a
        `country:<ISO3>` selector, a bare alpha-3 code, or a friendly
        country name).

        Args:
            token: The resolved Protected Planet token.
            selector: A WDPA id, `"country:<ISO3>"`, bare ISO3, or country
                name.

        Returns:
            gpd.GeoDataFrame: The selector's protected-area polygons.
        """
        text = selector.strip()
        if text.isdigit():
            return _rest.fetch_by_id(token, text)
        return _rest.fetch_country(token, self._catalog.resolve_iso3(text))

    def _api(self) -> FeatureCollection:
        """Fetch protected areas (satisfies the abstract contract)."""
        return FeatureCollection(self._fetch())

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> FeatureCollection:
        """Fetch the protected-area polygons and return the FeatureCollection.

        Args:
            progress_bar: Accepted for signature parity; the REST client
                has no progress bar, so this is a no-op.
            aggregate: Must be `None`. Protected areas are vector, not
                gridded; the facade already rejects a non-`None`
                `aggregate=` for a `vector` backend.

        Returns:
            FeatureCollection: The protected-area polygons, CRS
                `EPSG:4326`. Written to a file under `path` when set.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "WDPA.download(aggregate=...) is not supported: protected "
                "areas are vector polygons, not gridded rasters. Call "
                "download() without aggregate= and post-process the returned "
                "FeatureCollection (a GeoDataFrame) directly."
            )
        collection = FeatureCollection(self._fetch())
        if len(collection):
            warn_license(
                WDPA_LICENSE,
                "wdpa",
                detail="UNEP-WCMC terms: commercial use needs written permission "
                "and redistribution is restricted",
            )
        if self._user_path and len(collection):
            written = self._write(collection)
            logger.info(
                f"WDPA download summary: {len(collection)} protected area(s) "
                f"written to {written}"
            )
        else:
            logger.info(
                f"WDPA download summary: {len(collection)} protected area(s), "
                "nothing written"
            )
        return collection

    def _write(self, collection: FeatureCollection) -> Path:
        """Write the protected areas to a vector file under `root_dir`.

        Args:
            collection: The protected-area FeatureCollection.

        Returns:
            Path: Absolute path of the file written.
        """
        driver, ext = _FORMATS[self._file_format]
        out_path = self.root_dir / f"wdpa_protected_areas.{ext}"
        if driver == "parquet":
            collection.to_parquet(str(out_path))
        else:
            collection.to_file(str(out_path), driver=driver)
        return out_path
