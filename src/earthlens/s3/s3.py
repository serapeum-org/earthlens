"""AWS Open-Data S3 backend.

`S3` fetches public AWS Open-Data datasets over unsigned S3. A request
picks a `dataset` (a registered name such as `"era5"`, `"sentinel-2-l2a"`,
`"goes"`, `"copernicus-dem"`, `"esa-worldcover"`, or an inline spec dict
for the passthrough path) and a uniform set of selectors (`variables`,
the lat/lon bbox, and the date window). The backend resolves the dataset
to its bucket + key layout via the registry catalog, lists/derives the
S3 keys with the per-dataset resolver, downloads each granule (unsigned),
and crops/reprojects it to the AOI with pyramids — so every dataset is
driven through one code path and returns the same `list[Path]`.

The download orchestration uses the search/fetch split:
:meth:`S3._search` plans the products (cheap, no bulk transfer) and
:meth:`S3._fetch` downloads + localises them. Multi-tile AOIs (Copernicus
DEM / ESA WorldCover spanning more than one tile) currently yield one
cropped file per tile; merging them into a single mosaic is deferred to
the pyramids `PY-1` port.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.s3.auth import S3Auth, S3Credentials
from earthlens.s3.catalog import Catalog, Dataset
from earthlens.s3.layouts import plan_products

__all__ = ["S3"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    """Sanitise a product id into a filesystem-safe file stem."""
    return _UNSAFE.sub("_", value).strip("_")


class S3(AbstractDataSource):
    """Unified backend for public AWS Open-Data datasets over unsigned S3.

    Args:
        start: Inclusive start date string (parsed with `fmt`).
        end: Inclusive end date string.
        lat_lim: `[lat_min, lat_max]` AOI bounds in degrees.
        lon_lim: `[lon_min, lon_max]` AOI bounds in degrees.
        dataset: A registered dataset name (`"era5"`, `"sentinel-2-l2a"`,
            `"goes"`, `"copernicus-dem"`, `"esa-worldcover"`) or an inline
            spec dict for the passthrough path.
        variables: Variable / band tokens (friendly names, aliases, or raw
            native tokens). `None` uses the dataset's default variables.
        temporal_resolution: `"monthly"` or `"daily"`; controls the date
            stepping for temporal datasets (ignored for static ones).
        path: Output directory for the cropped files.
        fmt: `strptime` format for `start` / `end`.
        bucket: Optional override of the dataset's bucket (e.g. switch
            Copernicus DEM to the 90 m bucket, or GOES to `noaa-goes18`).
        output_format: `"geotiff"` to convert NetCDF output to GeoTIFF;
            `None` keeps the dataset's native format.
        aws_profile: Optional named AWS profile for signed access; the
            default is unsigned public access.

    Examples:
        - Resolve which products a request would download (no transfer):
            ```python
            >>> from earthlens.s3 import S3
            >>> src = S3(
            ...     start="2021-01-01", end="2021-01-01",
            ...     lat_lim=[0.4, 0.6], lon_lim=[6.4, 6.6],
            ...     dataset="copernicus-dem",
            ... )
            >>> products = src._search()
            >>> products[0].href
            'Copernicus_DSM_COG_10_N00_00_E006_00_DEM/Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif'

            ```
    """

    OUTPUT_KIND = "mixed"

    def __init__(
        self,
        start: str,
        end: str,
        lat_lim: list[float],
        lon_lim: list[float],
        dataset: str | dict[str, Any] = "era5",
        variables: list[str] | str | None = None,
        temporal_resolution: str = "monthly",
        path: str = "",
        fmt: str = "%Y-%m-%d",
        bucket: str | None = None,
        output_format: str | None = None,
        aws_profile: str | None = None,
    ):
        self._catalog = Catalog()
        resolved = self._catalog.resolve(dataset)
        if bucket:
            resolved = resolved.model_copy(update={"bucket": bucket})
        self._dataset: Dataset = resolved
        self._output_format = output_format
        self._aws_profile = aws_profile

        if variables is None:
            variables = list(self._dataset.default_variables)
        elif isinstance(variables, str):
            variables = [variables]

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

    # -- abstract hooks ------------------------------------------------

    def _initialize(self, *args: Any, **kwargs: Any) -> object:
        """Build the unsigned (or profile-signed) S3 client via `S3Auth`."""
        self._auth = S3Auth(S3Credentials(aws_profile=self._aws_profile))
        return self._auth.client()

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Capture the AOI bbox as a `SpatialExtent`."""
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Build the request date index, honouring static-vs-temporal datasets."""
        start_date = dt.datetime.strptime(start, fmt)
        end_date = dt.datetime.strptime(end, fmt)
        if self._dataset.temporal == "static":
            resolution = "MS"
            dates = pd.DatetimeIndex([start_date])
        else:
            resolution = "MS" if temporal_resolution == "monthly" else "D"
            dates = pd.date_range(start_date, end_date, freq=resolution)
            if len(dates) == 0:
                dates = pd.DatetimeIndex([start_date])
        return TemporalExtent(
            start_date=start_date,
            end_date=end_date,
            resolution=resolution,
            dates=dates,
        )

    # -- search / fetch ------------------------------------------------

    def _bbox(self) -> tuple[float, float, float, float]:
        """Return the AOI as `(west, south, east, north)`."""
        return (
            self.space.longitude_min,
            self.space.latitude_min,
            self.space.longitude_max,
            self.space.latitude_max,
        )

    def _search(self) -> list[RemoteProduct]:
        """Plan the S3 products satisfying this request (no bulk transfer)."""
        keys = self.vars if isinstance(self.vars, list) else [self.vars]
        variables = self._dataset.resolve_variables(keys)
        return plan_products(
            self._dataset, variables, self._bbox(), list(self.time.dates),
            self._auth.client(),
        )

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each product (idempotent) and crop it to the AOI.

        Objects that are absent (e.g. Copernicus DEM / WorldCover tiles
        that fall entirely over ocean) are logged and skipped; if every
        product is missing, a `RuntimeError` is raised so a wholly empty
        result is not silently returned.
        """
        client = self._auth.client()
        raw_dir = self.root_dir / "_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        default_ext = ".nc" if self._dataset.format == "netcdf" else ".tif"
        written: list[Path] = []
        missing = 0
        for product in tqdm(products, desc="amazon-s3", disable=not products):
            ext = Path(product.href).suffix or default_ext
            raw = raw_dir / f"{_safe_name(product.id)}{ext}"
            if not raw.exists():
                try:
                    client.download_file(
                        product.metadata["bucket"], product.href, str(raw)
                    )
                except Exception as exc:  # noqa: BLE001 - classified below
                    if _is_missing_object(exc):
                        logger.warning(
                            f"object not found, skipping: "
                            f"s3://{product.metadata['bucket']}/{product.href}"
                        )
                        missing += 1
                        continue
                    raise RuntimeError(
                        f"failed to download "
                        f"s3://{product.metadata['bucket']}/{product.href}: {exc}"
                    ) from exc
            written.append(self._localise(raw, product))
        if not written and missing:
            raise RuntimeError(
                f"none of the {missing} planned object(s) exist for this "
                "request; check the bbox / date window / variables."
            )
        return written

    def _localise(self, raw: Path, product: RemoteProduct) -> Path:
        """Read a downloaded granule, reproject/wrap if needed, crop to the AOI, write.

        Args:
            raw: The downloaded source file.
            product: The `RemoteProduct` that produced `raw` (its `id`
                names the output file).

        Returns:
            The path of the cropped output file.
        """
        from pyramids.dataset import Dataset as PyramidsDataset
        from pyramids.netcdf import NetCDF

        reader = NetCDF if self._dataset.format == "netcdf" else PyramidsDataset
        data = reader.read_file(str(raw))
        if self._dataset.lon_convention == "0-360":
            data = data.convert_longitude()
        if self._dataset.crs != 4326:
            # crs is None (per-file UTM / geostationary) -> warp to WGS84.
            data = data.to_crs(4326)
        west, south, east, north = self._bbox()
        # touch=False clips to the bbox extent; the default touch=True keeps
        # every cell that touches the mask and leaves the extent uncropped.
        data = data.crop(bbox=[west, south, east, north], epsg=4326, touch=False)

        as_geotiff = self._output_format == "geotiff" or self._dataset.format == "cog"
        out_ext = ".tif" if as_geotiff else ".nc"
        out_path = self.path / f"{_safe_name(product.id)}{out_ext}"
        data.to_file(str(out_path))
        return out_path

    # -- public API ----------------------------------------------------

    def _api(self, *args: Any, **kwargs: Any) -> list[Path]:
        """Compose `_search` + `_fetch` (the canonical post-C3 body)."""
        return self._api_via_search_fetch()

    def download(
        self, progress_bar: bool = True, aggregate: Any | None = None
    ) -> list[Path]:
        """Download every planned product, cropped to the AOI.

        Args:
            progress_bar: Show a per-product progress bar.
            aggregate: Optional `AggregationConfig` (NetCDF datasets only;
                wired in C5). Rejected for COG datasets.

        Returns:
            The cropped output file paths.

        Raises:
            NotImplementedError: If `aggregate` is given (wired in C5).
        """
        paths = self._api_via_search_fetch()
        if aggregate is not None:
            return self._aggregate(paths, aggregate)
        return paths

    def _aggregate(self, paths: list[Path], aggregate: Any) -> list[Path]:
        """Window-aggregate the cropped NetCDFs (wired in C5)."""
        raise NotImplementedError(
            "aggregate= support for the S3 backend is wired in task C5."
        )


def _is_missing_object(exc: BaseException) -> bool:
    """Classify an S3 download error as a missing-object (404/NoSuchKey/403)."""
    response = getattr(exc, "response", None)
    code = (response or {}).get("Error", {}).get("Code", "")
    return code in {"404", "NoSuchKey", "NoSuchBucket", "403", "AccessDenied"} or (
        "Not Found" in str(exc)
    )
