"""Global topography / bathymetry DEM backend — `Bathymetry(AbstractDataSource)`.

`Bathymetry` is a download-and-localise raster backend (`OUTPUT_KIND="raster"`).
A `dataset=<id>` selects a curated :class:`~earthlens.bathymetry.catalog.Dataset`
row (an ERDDAP `griddap` endpoint + coverage id + elevation band); `download()`
subsets that global DEM to the request bbox, writes the NetCDF, reads it back
with `pyramids.netcdf.NetCDF`, and writes a GeoTIFF — returning the list of
written GeoTIFF paths.

Every shipped DEM is static (no time axis), so the facade-forwarded
`aggregate=` is rejected: there is no temporal field to reduce. The NetCDF is
read **only** through pyramids — earthlens never imports a competing array
stack to touch the NetCDF. An
out-of-coverage / oversize request surfaces as a clear `ValueError` (a raster
has no "zero pixels" — and ERDDAP caps response size), never a bare HTTP error
or a corrupt file.

The DEMs are public, so there is no auth module.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.bathymetry._helpers import (
    bbox_from_extent,
    estimate_grid_pixels,
    griddap_subset_url,
)
from earthlens.bathymetry.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

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

    Fetches a curated static DEM (GEBCO 2020, ETOPO1 ice / bedrock) subset to
    the request bbox through one uniform ERDDAP `griddap` transport, written
    as GeoTIFF. The request is a search/fetch split: :meth:`_search` names the
    single resolved product and :meth:`_fetch` realises it (download → pyramids
    → GeoTIFF).

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

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        dataset: str = "",
        variables: list[str] | None = None,
        temporal_resolution: str = "static",
        path: Path | str = "",
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
            dataset: The curated DEM id to fetch (`"gebco_2020"`,
                `"etopo1_ice"`, `"etopo1_bedrock"`). Required.
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

        resolved_variables = (
            list(variables) if variables else [self._dataset.variable]
        )
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

    def _initialize(self):
        """No client / auth — the DEM servers are public (returns `None`)."""
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a `SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
        return TemporalExtent(
            start_date=None,
            end_date=None,
            resolution=temporal_resolution or "static",
            dates=pd.DatetimeIndex([]),
        )

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

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
        """Subset one DEM to NetCDF, then write a GeoTIFF via pyramids.

        Builds the griddap subset URL, GETs the NetCDF (validating the magic
        bytes), then `pyramids.netcdf.NetCDF` reads it and writes the
        elevation band to GeoTIFF.

        Args:
            product: The `RemoteProduct` whose `metadata["dataset"]` is the
                resolved catalog row.

        Returns:
            Path: The written GeoTIFF at `<root_dir>/<id>.tif`.

        Raises:
            ValueError: When the server rejects the request or returns a
                non-NetCDF body (out-of-coverage / oversize bbox).
        """
        row: Dataset = product.metadata["dataset"]
        bbox = bbox_from_extent(self.space)
        self._log_estimated_size(row, bbox)
        url = griddap_subset_url(
            row.endpoint, row.dataset_id, row.variable, bbox, row.lon_convention
        )
        nc_path = self.root_dir / f"{row.id}.nc"
        tif_path = self.root_dir / f"{row.id}.tif"
        logger.info(f"bathymetry {row.id}: GET {url}")
        content = self._download(url, row)
        nc_path.write_bytes(content)
        self._to_geotiff(nc_path, row.variable, tif_path)
        return tif_path

    def _download(self, url: str, row: Dataset) -> bytes:
        """GET the griddap `.nc` body, validating it is real NetCDF.

        Args:
            url: The griddap subset URL.
            row: The resolved catalog row (for error messages).

        Returns:
            bytes: The NetCDF body.

        Raises:
            ValueError: On any HTTP error, or a non-NetCDF body (an HTML
                error page, sometimes served with a 200) — typically an
                out-of-coverage or oversize bbox.
        """
        try:
            response = requests.get(url, timeout=self._timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise ValueError(
                f"bathymetry request for {row.id!r} failed over "
                f"{self._extent_label()}: {exc}. The bbox may be outside the "
                f"DEM's coverage, or too large for the server (shrink it)."
            ) from exc
        content = response.content
        if not content.startswith(_NETCDF_MAGIC):
            raise ValueError(
                f"bathymetry {row.id!r} returned a non-NetCDF body "
                f"({len(content)} bytes, starts {content[:24]!r}) over "
                f"{self._extent_label()}. The server likely returned an error "
                f"page instead of data; the bbox may be outside coverage or "
                f"too large (shrink it)."
            )
        return content

    @staticmethod
    def _to_geotiff(nc_path: Path, variable: str, tif_path: Path) -> None:
        """Read the NetCDF band with pyramids and write a GeoTIFF.

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
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the DEM subset(s) as written GeoTIFF(s).

        Args:
            progress_bar: Accepted for signature parity; one request per call.
            aggregate: Must be `None`. A static DEM has no temporal axis to
                reduce, so a non-`None` value raises `NotImplementedError`
                (the facade already gates this; this is the belt-and-suspenders
                guard for direct callers).

        Returns:
            list[Path]: The written GeoTIFF DEM subset path(s).

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
            ValueError: If a request is outside the DEM's coverage / oversize
                (from :meth:`_download`).
        """
        if aggregate is not None:
            raise NotImplementedError(
                "Bathymetry.download(aggregate=...) is not supported: a "
                "topography / bathymetry DEM is a single static grid with no "
                "temporal axis, so there is nothing to reduce. Call "
                "download() without aggregate=."
            )
        return self._api()
