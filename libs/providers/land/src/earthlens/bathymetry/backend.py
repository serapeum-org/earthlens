"""Global topography / bathymetry DEM backend — `Bathymetry(AbstractDataSource)`.

`Bathymetry` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`).
A `dataset=<id>` selects a curated :class:`~earthlens.bathymetry.catalog.Dataset`
row and `download()` subsets that DEM to the request bbox, returning the list of
written GeoTIFF paths. Two transports ship, chosen per row: an ERDDAP `griddap`
endpoint (GEBCO / ETOPO — subset to a NetCDF read back with
`pyramids.netcdf.NetCDF`) and an OGC WCS endpoint (EMODnet Bathymetry — read
through `pyramids.Dataset.from_wcs`). Either path writes a GeoTIFF.

Every shipped DEM is static (no time axis), so the facade-forwarded
`aggregate=` is rejected: there is no temporal field to reduce. The grid is
read **only** through pyramids — earthlens never imports a competing array
stack to touch it. On the griddap path an out-of-coverage / oversize request
surfaces as a clear `ValueError` (ERDDAP rejects it or returns an HTML error
body, never data), so the user never sees a bare HTTP error or a corrupt file.
The WCS path instead guards the request bbox against the coverage's
`native_bbox` (:meth:`Bathymetry._guard_wcs_domain`) — a fully out-of-coverage
AOI raises, a partial overlap is allowed through with a warning (its
out-of-coverage cells come back as `0.0` fill) — because the WCS server returns
a zero-filled grid, not an error, outside coverage.

The DEMs are public, so there is no auth module.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import cast

import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    mask_to_geometry,
)
from earthlens.base.http import HttpClient
from earthlens.bathymetry._helpers import (
    WcsServiceUnavailableError,
    bbox_from_extent,
    estimate_grid_pixels,
    griddap_subset_url,
    is_wcs_service_failure,
)
from earthlens.bathymetry.catalog import Catalog, Dataset

#: Leading magic bytes of the NetCDF container formats ERDDAP serves —
#: classic NetCDF-3 (`CDF\x01/02/05`) and NetCDF-4/HDF5 (`\x89HDF`). A griddap
#: body that does not start with one of these is an error page (ERDDAP serves
#: those as HTML, sometimes with a 200), not data.
_NETCDF_MAGIC: tuple[bytes, ...] = (b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF")

#: Pixel-count threshold past which a subset is flagged as large in the log
#: (≈ a 0.25-gigapixel grid). The server enforces the hard cap; this is an
#: early heads-up for the user.
_LARGE_PIXEL_THRESHOLD = 250_000_000


class Bathymetry(AbstractDataSource):
    """Global topography / bathymetry DEM backend (raster GeoTIFF output).

    Fetches a curated static DEM (GEBCO 2020, ETOPO1 ice / bedrock via ERDDAP
    `griddap`; EMODnet Bathymetry via OGC WCS) subset to the request bbox and
    written as GeoTIFF. The request is a search/fetch split: :meth:`_search`
    names the single resolved product and :meth:`_fetch` realises it (download
    → pyramids → GeoTIFF), dispatching on the row's `transport`.

    Attributes:
        OUTPUT_KIND: Fixed `"raster"`; every DEM yields a gridded GeoTIFF.
            The facade reads it to gate `aggregate=` (rejected here — the DEMs
            are static).

    Examples:
        - A GEBCO subset writes one GeoTIFF (marked `+SKIP` — it hits the live
          NOAA CoastWatch ERDDAP):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="bathymetry",
            ...     dataset="gebco_2020",
            ...     lat_lim=[25.0, 26.0],
            ...     lon_lim=[-18.0, -17.0],
            ...     path="bathy_out",
            ... ).download()  # -> [Path('bathy_out/gebco_2020.tif')]

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "a topography / bathymetry DEM is a single static grid with no temporal axis, so there is nothing to reduce. Call download() without aggregate="

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: The bathymetry grids are time-invariant, so a missing `start` / `end` is legal
    #: here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        dataset: str = "",
        variables: list[str] | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        timeout: float = 120.0,
        catalog: Catalog | None = None,
    ):
        """Initialise a bathymetry backend instance.

        Resolves `dataset=` against the catalog and validates any requested
        `variables` against the row's single elevation band **before** the
        parent constructor runs.

        Args:
            start: Accepted for facade parity; ignored (the DEMs are static).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes. Required.
            dataset: The curated DEM id to fetch — a global ERDDAP DEM
                (`"gebco_2020"`, `"etopo1_ice"`, `"etopo1_bedrock"`) or an
                EMODnet Bathymetry WCS row (`"emodnet"` for the latest release,
                or a year-stamped `"emodnet_2022"` / `"_2020"` / `"_2018"` /
                `"_2016"`). Required.
            variables: Optional band name(s); defaults to the row's single
                elevation band. A name other than that band raises with a
                did-you-mean.
            temporal_resolution: Advisory label only (the DEMs are static).
            path: Output directory for the written `.nc` / `.tif`.
            fmt: Accepted for facade parity; unused.
            timeout: Per-request HTTP timeout in seconds for the download.
            catalog: Optional pre-built `Catalog` (tests inject a faked one);
                defaults to the bundled catalog.

        Raises:
            ValueError: If `dataset` is empty / unknown (did-you-mean
                surfaced), a requested variable is not the row's band, or the
                bounding box is missing.
            TypeError: If `variables` is a mapping (the band is named by
                `dataset=`, not a per-dataset map).
        """
        if not dataset:
            raise ValueError(
                "Bathymetry requires dataset=<id> naming a curated DEM "
                "(e.g. dataset='gebco_2020'). List ids with "
                "earthlens.bathymetry.Catalog().datasets."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "Bathymetry `variables` must be a list of band names (or "
                "omitted), not a mapping. Name the DEM with dataset=<id>."
            )
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                "Bathymetry requires a bounding box (lat_lim=[s, n], "
                "lon_lim=[w, e]) — a DEM subset has no default global extent."
            )

        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset: Dataset = self._catalog.get(dataset)
        self._timeout = timeout

        resolved_variables = list(variables) if variables else [self._dataset.variable]
        self._validate_variables(resolved_variables)

        super().__init__(
            start=start,
            end=end,
            variables=resolved_variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _validate_variables(self, variables: list[str]) -> None:
        """Check every requested variable is the row's elevation band.

        Args:
            variables: The resolved band name(s).

        Raises:
            ValueError: If a name is not the row's single band; the message
                offers the band as a did-you-mean.
        """
        band = self._dataset.variable
        for name in variables:
            if name != band:
                hint = ""
                if difflib.get_close_matches(name, [band], n=1):
                    hint = f" Did you mean {band!r}?"
                raise ValueError(
                    f"{name!r} is not a band of {self._dataset.id!r}; its only "
                    f"elevation band is {band!r}.{hint}"
                )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a degenerate (timeless) extent — the DEMs are static.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty
                date index (a static DEM has no time axis).
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    def _search(self) -> list[RemoteProduct]:
        """Name the single resolved product (one DEM per request).

        Returns:
            list[RemoteProduct]: One product carrying the resolved
                :class:`Dataset` row in its `metadata`.
        """
        return [
            RemoteProduct(
                id=self._dataset.dataset_id,
                metadata={"dataset": self._dataset},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each DEM subset and write it as a GeoTIFF.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list[Path]: The written GeoTIFF path(s).
        """
        return [self._fetch_one_dem(product) for product in products]

    def _fetch_one_dem(self, product: RemoteProduct) -> Path:
        """Subset one DEM to a GeoTIFF via the row's transport.

        Dispatches on the resolved row's `transport`: `"wcs"` rows are read
        over OGC WCS through pyramids `Dataset.from_wcs` (:meth:`_fetch_wcs`),
        every other row over ERDDAP `griddap` (:meth:`_fetch_griddap`).

        Args:
            product: The `RemoteProduct` whose `metadata["dataset"]` is the
                resolved catalog row.

        Returns:
            Path: The written GeoTIFF at `<root_dir>/<id>.tif`.

        Raises:
            ValueError: When the server rejects the request, returns a
                non-NetCDF body, or the request falls outside a WCS row's
                coverage (out-of-coverage / oversize bbox).
        """
        row: Dataset = product.metadata["dataset"]
        bbox = bbox_from_extent(self.space)
        tif_path = self.root_dir / f"{row.id}.tif"
        if row.transport == "wcs":
            self._fetch_wcs(row, bbox, tif_path)
        else:
            self._fetch_griddap(row, bbox, tif_path)
        return tif_path

    def _fetch_griddap(
        self, row: Dataset, bbox: tuple[float, float, float, float], tif_path: Path
    ) -> None:
        """Subset an ERDDAP `griddap` DEM to NetCDF, then write a GeoTIFF.

        Builds the griddap subset URL, GETs the NetCDF (validating the magic
        bytes), then `pyramids.netcdf.NetCDF` reads it and writes the
        elevation band to GeoTIFF.

        Args:
            row: The resolved `griddap` catalog row.
            bbox: `(west, south, east, north)` of the request in degrees.
            tif_path: Destination GeoTIFF path.

        Raises:
            ValueError: When the server rejects the request or returns a
                non-NetCDF body (out-of-coverage / oversize bbox).
        """
        self._log_estimated_size(row, bbox)
        url = griddap_subset_url(
            row.endpoint, row.dataset_id, row.variable, bbox, row.lon_convention
        )
        nc_path = self.root_dir / f"{row.id}.nc"
        logger.info(f"bathymetry {row.id}: GET {url}")
        self._download(url, row, nc_path)
        self._to_geotiff(nc_path, row.variable, tif_path)

    def _fetch_wcs(
        self, row: Dataset, bbox: tuple[float, float, float, float], tif_path: Path
    ) -> None:
        """Read a WCS-transport DEM through pyramids `from_wcs`, write a GeoTIFF.

        Guards the request against the coverage's advertised extent first (a
        WCS server returns an all-zeros grid, not an error, outside coverage),
        then delegates the `GetCoverage` request, subset, and read entirely to
        `pyramids.Dataset.from_wcs` — earthlens supplies only the coverage id,
        AOI bbox, CRS, and protocol version. A polygon `aoi=` is applied with
        the shared `mask_to_geometry` (a no-op without one), matching the
        griddap path.

        Args:
            row: The resolved `wcs` catalog row.
            bbox: `(west, south, east, north)` of the request in `row.crs`.
            tif_path: Destination GeoTIFF path.

        Raises:
            WcsServiceUnavailableError: When `from_wcs` fails for a transport /
                service reason (a dropped connection, a 5xx / gateway error, or
                a non-XML `GetCapabilities` answer) — a distinct type so a live
                `e2e` test can skip on a flaky upstream instead of failing.
            ValueError: When the request itself is at fault — the bbox is outside
                the coverage extent, or the coverage / subset is otherwise
                invalid.
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        self._guard_wcs_domain(row, bbox)
        self._log_estimated_size(row, bbox)
        logger.info(
            f"bathymetry {row.id}: WCS GetCoverage {row.endpoint} "
            f"coverage={row.dataset_id} bbox={bbox}"
        )
        try:
            dataset = PyramidsDataset.from_wcs(
                row.endpoint,
                coverage=row.dataset_id,
                bbox=bbox,
                crs=row.crs,
                version=row.wcs_version or None,
                timeout=self._timeout,
            )
        except Exception as exc:
            if is_wcs_service_failure(exc):
                raise WcsServiceUnavailableError(
                    f"the WCS service at {row.endpoint} is unavailable for "
                    f"{row.id!r} over {self._extent_label()}: {exc}. This is a "
                    f"server-side / transport problem, not the request — retry "
                    f"later."
                ) from exc
            raise ValueError(
                f"bathymetry WCS request for {row.id!r} failed over "
                f"{self._extent_label()}: {exc}. The bbox may be outside the "
                f"coverage or too large for the server (shrink it)."
            ) from exc
        dataset = mask_to_geometry(dataset, self.space)
        dataset.to_file(str(tif_path))

    def _guard_wcs_domain(
        self, row: Dataset, bbox: tuple[float, float, float, float]
    ) -> None:
        """Reject or warn about a WCS request that leaves the coverage extent.

        The EMODnet WCS server returns a zero-filled grid (not an error) for an
        AOI outside its coverage. A **fully disjoint** bbox is turned into a
        clear error pointing at the global DEMs; a bbox that only **partially**
        overlaps the coverage is allowed through (the in-coverage cells are
        real) but logs a warning, because its out-of-coverage cells come back as
        `0.0` (sea-level) fill indistinguishable from a genuine 0 m reading.

        The numeric comparison assumes the request bbox and `native_bbox` share
        `EPSG:4326` lon/lat degrees (true for every shipped row); a future row
        in a projected CRS is skipped rather than mis-guarded on mixed units.

        Args:
            row: The resolved `wcs` catalog row (carries `native_bbox`).
            bbox: `(west, south, east, north)` of the request in `row.crs`.

        Raises:
            ValueError: If the request bbox does not intersect `native_bbox`.
        """
        extent = row.native_bbox
        if extent is None:
            return
        if row.crs != "EPSG:4326":
            # native_bbox and the request bbox are only comparable in lon/lat
            # degrees; skip the numeric guard for any other CRS.
            return
        west, south, east, north = bbox
        ext_west, ext_south, ext_east, ext_north = extent
        disjoint = (
            east <= ext_west
            or west >= ext_east
            or north <= ext_south
            or south >= ext_north
        )
        if disjoint:
            raise ValueError(
                f"request bbox {bbox} is outside the {row.id!r} coverage extent "
                f"{extent} (European seas / NE Atlantic). Use a global DEM "
                f"(dataset='gebco_2020' or 'etopo1_ice') for this area."
            )
        outside = (
            west < ext_west or east > ext_east or south < ext_south or north > ext_north
        )
        if outside:
            logger.warning(
                f"bathymetry {row.id}: request bbox {bbox} extends beyond the "
                f"coverage extent {extent}; cells outside the coverage return as "
                f"0.0 (sea-level) fill, not real depths. Shrink the bbox to the "
                f"coverage, or use a global DEM (gebco_2020 / etopo1_ice) for the "
                f"out-of-domain area."
            )

    def _client(self) -> HttpClient:
        """Return this instance's HTTP client, built once.

        Held on the instance so a request spanning several tiles reuses one
        pooled connection instead of re-handshaking per file.

        Returns:
            HttpClient: The shared client.
        """
        if self._http is None:
            self._http = HttpClient(
                timeout=self._timeout,
                max_retries=0,
                status_forcelist=(),
                raise_for_status=True,
            )
        return self._http

    def _download(self, url: str, row: Dataset, dest: Path) -> Path:
        """Stream the griddap `.nc` body to `dest`, validating it is real NetCDF.

        The body is written straight to disk rather than materialised as
        `bytes` and then written: a DEM subset can be hundreds of megabytes,
        and buffering the whole thing to copy it doubles the peak memory for
        no benefit.

        Args:
            url: The griddap subset URL.
            row: The resolved catalog row (for error messages).
            dest: The `.nc` path to write.

        Returns:
            Path: The `dest` path the NetCDF was written to.

        Raises:
            ValueError: On any HTTP error, or a non-NetCDF body (an HTML
                error page, sometimes served with a 200) — typically an
                out-of-coverage or oversize bbox.
        """
        http = self._client()
        try:
            http.download(url, dest, progress=False, expect_magic=_NETCDF_MAGIC)
        except requests.exceptions.RequestException as exc:
            raise ValueError(
                f"bathymetry request for {row.id!r} failed over "
                f"{self._extent_label()}: {exc}. The bbox may be outside the "
                f"DEM's coverage, or too large for the server (shrink it)."
            ) from exc
        except ValueError as exc:
            raise ValueError(
                f"bathymetry {row.id!r} returned a non-NetCDF body over "
                f"{self._extent_label()}: {exc} The bbox may be outside "
                f"coverage or too large (shrink it)."
            ) from exc
        return dest

    def _to_geotiff(self, nc_path: Path, variable: str, tif_path: Path) -> None:
        """Read the NetCDF band with pyramids, mask to the AOI, write a GeoTIFF.

        The ERDDAP `griddap` server already subsets to the request *bbox*, so
        no client-side bbox crop is needed. A polygon `aoi=`, however, is only
        expressed to the server as its bounding box — so the exact polygon is
        applied here with the shared `mask_to_geometry` (a no-op when the
        request carries no polygon), matching every other raster backend.

        Args:
            nc_path: The downloaded NetCDF subset.
            variable: The elevation band name to extract.
            tif_path: Destination GeoTIFF path.

        Raises:
            ValueError: Propagated from pyramids if the band is absent /
                unreadable.
        """
        from pyramids.netcdf import NetCDF

        nc = NetCDF.read_file(str(nc_path))
        band = nc.get_variable(variable)
        band = mask_to_geometry(band, self.space)
        band.to_file(str(tif_path))

    def _log_estimated_size(
        self, row: Dataset, bbox: tuple[float, float, float, float]
    ) -> None:
        """Log the estimated subset pixel dimensions, warn past a threshold.

        Args:
            row: The resolved catalog row.
            bbox: `(west, south, east, north)` of the request.
        """
        dims = estimate_grid_pixels(bbox, row.native_resolution)
        if dims is None:
            return
        width, height = dims
        total = width * height
        message = (
            f"bathymetry {row.id}: ~{width}x{height} px "
            f"({total:,} cells) at {row.native_resolution}"
        )
        if total > _LARGE_PIXEL_THRESHOLD:
            logger.warning(
                message + " — large subset; the server may reject it. "
                "Consider a smaller bbox."
            )
        else:
            logger.info(message)

    def _extent_label(self) -> str:
        """Render the request bbox for error / warning messages."""
        return (
            f"bbox [{self.space.west}, {self.space.south}, "
            f"{self.space.east}, {self.space.north}]"
        )

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the DEM subset(s) as written GeoTIFF(s).

        Args:
            progress_bar: Accepted for signature parity; one request per call.

        Returns:
            list[Path]: The written GeoTIFF DEM subset path(s).

        Raises:
            ValueError: If a request is outside the DEM's coverage / oversize
                (from :meth:`_download`).
        """
        return cast("list[Path]", self._api())
