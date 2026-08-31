"""Copernicus DEM backend — anonymous, no account, raw COG tiles.

`DEM(AbstractDataSource)` fetches Copernicus DEM GLO-30 / GLO-90 tiles
from the anonymous AWS Open Data buckets `copernicus-dem-30m` and
`copernicus-dem-90m`. The backend is unusual on two counts:

* **Zero credentials.** The buckets are public and unsigned; every
  other shipped path to a global DEM (`gee`, `stac`, `earthdata`)
  requires an account. This backend earns its place by offering
  Copernicus DEM without one — the whole `G0` justification.
* **No decode.** A request downloads the raw 1° x 1° COG GeoTIFF(s) as
  shipped by the bucket and returns their `list[Path]`. Cropping,
  reprojecting, and mosaicking are pyramids' job (its COG reader
  already reads and mosaics COGs). This module does not import
  `rasterio`, `gdal`, `osgeo`, or `xarray`.

Request shape: a `dataset` key (`"cop-dem-glo-30"` default, or
`"cop-dem-glo-90"`) and a WGS84 bbox (`lat_lim`, `lon_lim`). The
backend is time-invariant — the `start` / `end` dates are accepted for
the shared `AbstractDataSource` signature but are advisory only. Ocean
and outside-coverage tiles are absent from the bucket and are logged
and skipped, never fatal (`G6`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.base.s3 import S3Auth, S3Credentials
from earthlens.dem._helpers import Tile, bbox_to_tiles, tile_key
from earthlens.dem.catalog import Catalog, DEMDataset

__all__ = ["DEM"]


class DEM(AbstractDataSource):
    """Anonymous Copernicus DEM backend (raw COG tiles).

    Args:
        start: Advisory start date (parsed with `fmt`); DEM is
            time-invariant, so an omitted `start` defaults to today.
        end: Advisory end date; defaults to `start`.
        variables: Ignored — a Copernicus DEM tile carries one
            elevation band. Kept in the signature for `AbstractDataSource`
            compatibility.
        lat_lim: `[lat_min, lat_max]` in degrees. Selects which 1° tiles
            to fetch.
        lon_lim: `[lon_min, lon_max]` in degrees.
        temporal_resolution: Advisory label; DEM has no time cadence.
        path: Output directory for the downloaded tiles.
        fmt: `strptime` format for `start` / `end`.
        dataset: Catalog key — `"cop-dem-glo-30"` (default) or
            `"cop-dem-glo-90"`.
        catalog: Optional pre-built :class:`Catalog` (tests inject
            one); defaults to the bundled catalog.

    Examples:
        - Plan the tiles a bbox needs without touching the network:
            ```python
            >>> from earthlens.dem import DEM
            >>> src = DEM(
            ...     start="2026-01-01", end="2026-01-01",
            ...     variables=[],
            ...     lat_lim=[30.2, 30.8], lon_lim=[31.2, 31.8],
            ... )
            >>> [(t.lat, t.lon) for t in src.tiles()]
            [(30, 31)]

            ```
    """

    OUTPUT_KIND = "raster"

    AGGREGATE_REFUSAL_REASON = "a DEM tile is time-invariant. Mosaic / crop the downloaded tiles with pyramids downstream (`pyramids.Dataset.read_file` + `.crop`, `pyramids.dataset.merge.merge_rasters` for a multi-tile mosaic)"

    #: Elevation is time-invariant, so a missing `start` / `end` is legal here.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: list[str] | dict[str, list[str]] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        dataset: str = "cop-dem-glo-30",
        catalog: Catalog | None = None,
    ):
        """Initialise a Copernicus DEM backend instance.

        Raises:
            ValueError: If `dataset` is not a curated DEM catalog key.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset_key = dataset
        self._dataset: DEMDataset = self._catalog.get_dataset(dataset)
        self._show_progress = True
        #: Set by `download(force=...)`; bypasses the skip-if-exists check.
        self._force = False
        # `_initialize` (called by `super().__init__` below) builds `self._auth`.

        super().__init__(
            start=start or "1970-01-01",
            end=end or start or "1970-01-01",
            variables=variables or [],
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim if lat_lim is not None else [-90.0, 90.0],
            lon_lim=lon_lim if lon_lim is not None else [-180.0, 180.0],
            fmt=fmt,
            path=path,
        )

    # -- abstract hooks -------------------------------------------------

    def _initialize(self) -> None:
        """Build the unsigned `boto3` client via the shared `S3Auth`.

        The client is lazy: `S3Auth.client()` builds it on first access
        and caches it. Returning `None` here avoids overwriting
        `self.client` — the auth object stays the source of truth.

        Returns:
            None: The auth object owns the client (accessed via
                :meth:`_client`).
        """
        self._auth = S3Auth(
            S3Credentials(region=self._dataset.region),
        )
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a single-instant :class:`TemporalExtent` (DEM is static).

        DEM has no time axis; the returned extent carries a single-date
        `DatetimeIndex` derived from `start` so the shared
        `TemporalExtent` validator passes.
        """
        start_date = to_datetime(start, fmt)
        end_date = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_date,
            end_date=end_date,
            resolution="static",
            dates=pd.DatetimeIndex([start_date]),
        )

    # -- planning + fetch -----------------------------------------------

    def tiles(self) -> list[Tile]:
        """Return the 1° tiles the request bbox intersects.

        Pure arithmetic on the integer-degree grid — no network access.
        Antimeridian-straddling bboxes (`lon_min > lon_max`) are
        rejected at construction; pass the two halves in separate calls
        if you need to span the 180th meridian.

        Returns:
            list[Tile]: One tile per SW-corner integer degree pair in
                the bbox, in row-major (south → north, west → east)
                order.
        """
        return bbox_to_tiles(
            lat_min=self.space.latitude_min,
            lat_max=self.space.latitude_max,
            lon_min=self.space.longitude_min,
            lon_max=self.space.longitude_max,
        )

    def _search(self) -> list[RemoteProduct]:
        """Enumerate one candidate product per 1° tile in the bbox.

        The key is deterministic (no `list_objects_v2` call), so a
        request with a wide bbox does not walk the bucket. Ocean /
        outside-coverage tiles are only discovered as missing when
        `_fetch` calls `head_object` on each candidate.

        Returns:
            list[RemoteProduct]: One :class:`RemoteProduct` per tile;
                `href` carries the S3 key and `metadata` carries the
                bucket, the tile identifier, and the SW corner.
        """
        token = self._dataset.resolution_token
        bucket = self._dataset.bucket
        products: list[RemoteProduct] = []
        for tile in self.tiles():
            key = tile_key(tile, token)
            products.append(
                RemoteProduct(
                    id=key.split("/", 1)[0],
                    href=key,
                    metadata={
                        "bucket": bucket,
                        "dataset": self._dataset_key,
                        "tile_lat": tile.lat,
                        "tile_lon": tile.lon,
                    },
                )
            )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download every present tile; log and skip ocean / missing ones.

        The bucket carries one COG per land tile; a candidate key for an
        ocean tile (or outside coverage) simply returns a 404 on
        `head_object`. Those are warned about and skipped rather than
        propagated, so a coastal bbox returns a ragged but non-empty
        result — never a crash.

        Args:
            products: The candidate products from :meth:`_search`.

        Returns:
            list[Path]: One local path per tile that existed and was
                downloaded, in `products` order.
        """
        written: list[Path] = []
        missing = 0
        for product in tqdm(
            products,
            disable=not (self._show_progress and products),
            desc=f"dem/{self._dataset_key}",
            unit="tile",
        ):
            fetched = self._fetch_one(product)
            if fetched is None:
                missing += 1
                continue
            written.append(fetched)
        if missing:
            logger.info(
                f"dem: {len(written)}/{len(products)} tile(s) downloaded; "
                f"{missing} absent (ocean / outside coverage)."
            )
        return written

    def _fetch_one(self, product: RemoteProduct) -> Path | None:
        """Head + download one candidate tile; return `None` if absent.

        Args:
            product: The candidate :class:`RemoteProduct` to fetch.

        Returns:
            Path | None: The written local path, or `None` when the key
                is absent (ocean tile, outside coverage).
        """
        client = self._client()
        bucket: str = product.metadata["bucket"]
        key: str = product.href or ""
        target = self.root_dir / Path(key).name
        if self._is_complete(target, force=self._force):
            return target
        try:
            client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - classify below
            if _is_missing_object(exc):
                logger.warning(f"dem: tile absent, skipping: s3://{bucket}/{key}")
                return None
            raise
        tmp = target.with_name(target.name + ".part")
        try:
            client.download_file(bucket, key, str(tmp))
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def _client(self) -> Any:
        """Return the unsigned `boto3` client (built lazily by `S3Auth`)."""
        return self._auth.client()

    def download(
        self,
        progress_bar: bool = True,
        *,
        force: bool = False,
    ) -> list[Path]:
        """Fetch every 1° Copernicus DEM tile the bbox intersects.

        Args:
            progress_bar: Show a per-tile progress bar. Defaults to
                `True`.
            force: Re-fetch even when a complete output already exists,
                bypassing the skip-if-exists check. Defaults to `False`.

        Returns:
            list[Path]: The local paths of the downloaded COG tiles, in
                bbox row-major order. Empty when every candidate tile is
                absent from the bucket.
        """
        self._show_progress = progress_bar
        self._force = force
        return self._api_via_search_fetch()


def _error_code(exc: BaseException) -> str:
    """Return the S3 error code from a botocore exception, or `""`.

    Args:
        exc: The exception raised by a `boto3` call.

    Returns:
        str: The `Error.Code` string, or `""` when absent.
    """
    response = getattr(exc, "response", None)
    return cast("str", (response or {}).get("Error", {}).get("Code", ""))


def _is_missing_object(exc: BaseException) -> bool:
    """Return whether `exc` classifies as a genuinely-absent S3 object.

    Anonymous `HeadObject` on a missing key raises `ClientError` with
    `Error.Code == "404"`, so the classifier only accepts that code (or
    the equivalent `"NoSuchKey"`). Auth (`403` / `AccessDenied`),
    bucket-level (`NoSuchBucket`), and network / DNS / endpoint errors
    all carry different codes and must surface to the caller — a
    string-substring `"Not Found"` fallback would silently misclassify
    "Host Not Found" / "Endpoint Not Found" as ocean tiles.

    Args:
        exc: The exception raised by `head_object` / `download_file`.

    Returns:
        bool: `True` for a 404 / NoSuchKey error, `False` for any other
            error class.
    """
    return _error_code(exc) in {"404", "NoSuchKey"}
