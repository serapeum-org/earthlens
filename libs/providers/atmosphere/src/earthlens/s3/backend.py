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

import re
from dataclasses import dataclass
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
    crop_to_aoi,
    to_datetime,
    warn_if_egress,
)
from earthlens.base.s3 import S3Auth, S3Credentials
from earthlens.s3.catalog import Catalog, Dataset
from earthlens.s3.layouts import plan_products

__all__ = ["S3"]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    """Sanitise a product id into a filesystem-safe file stem."""
    return _UNSAFE.sub("_", value).strip("_")


@dataclass(frozen=True)
class _AggregationVariable:
    """Duck-typed `var_info` for `aggregate_netcdf` (the fields it reads)."""

    nc_variable: str
    cds_variable: str
    is_flux: bool = False


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

    @classmethod
    def datasets(cls) -> list[str]:
        """Return the registered dataset names available to `dataset=`.

        Returns:
            The sorted registry names (`["copernicus-dem", "era5", ...]`).

        Examples:
            - Discover the bundled datasets:
                ```python
                >>> from earthlens.s3 import S3
                >>> S3.datasets()
                ['copernicus-dem', 'era5', 'esa-worldcover', 'goes', 'naip-source', 'sentinel-2-l2a', 'usgs-landsat']

                ```
        """
        return Catalog().dataset_names()

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
        scene: str | None = None,
        tile: str | None = None,
        max_scenes: int | None = None,
    ):
        """Resolve the dataset and wire up the request (see the class docstring for args).

        The constructor arguments are documented on the class. `scene` /
        `tile` supply the explicit identifier for the identifier-addressed
        datasets (Landsat scene id, NAIP quad path); `max_scenes` caps the
        Sentinel-2 scenes kept per (tile, month). They are carried onto the
        resolved dataset's `params` so the per-dataset resolver can read them.

        Raises:
            ValueError: If `dataset` is an unknown name or an inline spec that
                fails validation (propagated from `Catalog.resolve`).
        """
        self._catalog = Catalog()
        resolved = self._catalog.resolve(dataset)
        updates: dict[str, Any] = {}
        if bucket:
            updates["bucket"] = bucket
        # Scene/tile identifiers (Landsat scene id, NAIP quad path) and the
        # Sentinel-2 max_scenes cap feed the resolver via params.
        extra_params = {
            k: v
            for k, v in (("scene", scene), ("tile", tile), ("max_scenes", max_scenes))
            if v is not None
        }
        if extra_params:
            updates["params"] = {**resolved.params, **extra_params}
        if updates:
            resolved = resolved.model_copy(update=updates)
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
        """Build the S3 client via `S3Auth` (unsigned, or signed for requester-pays)."""
        self._auth = S3Auth(
            S3Credentials(
                aws_profile=self._aws_profile,
                signed=self._dataset.requester_pays,
                region=self._dataset.region,
            )
        )
        return self._auth.client()

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Capture the AOI bbox as a `SpatialExtent`."""
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Build the request date index, honouring static-vs-temporal datasets."""
        start_date = to_datetime(start, fmt)
        end_date = to_datetime(end, fmt)
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
        products = plan_products(
            self._dataset,
            variables,
            self._bbox(),
            list(self.time.dates),
            self._auth.client(),
        )
        # Surface the request volume so a wide AOI / long window is never a
        # silent surprise (e.g. many Sentinel-2 scenes).
        logger.info(
            f"amazon-s3: planned {len(products)} object(s) for {self._dataset.bucket}"
        )
        return products

    def _raw_dir(self) -> Path:
        """Return (creating if needed) the directory raw granules download to."""
        raw_dir = self.root_dir / "_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        return raw_dir

    def _download_raw(
        self, client: Any, product: RemoteProduct, raw_dir: Path
    ) -> Path | None:
        """Download one product to `raw_dir` (idempotent); `None` if absent.

        A missing object (404 / NoSuchKey — e.g. a DEM tile over ocean) is
        logged and returns `None`; any other error is re-raised wrapped so
        it is never silently swallowed.
        """
        default_ext = ".nc" if self._dataset.format == "netcdf" else ".tif"
        ext = Path(product.href).suffix or default_ext
        raw = raw_dir / f"{_safe_name(product.id)}{ext}"
        if raw.exists():
            return raw
        extra = {"RequestPayer": "requester"} if self._dataset.requester_pays else None
        try:
            client.download_file(
                product.metadata["bucket"], product.href, str(raw), ExtraArgs=extra
            )
        except Exception as exc:  # noqa: BLE001 - classified by S3 error code
            bucket = product.metadata["bucket"]
            if _is_missing_object(exc):
                logger.warning(
                    f"object not found, skipping: s3://{bucket}/{product.href}"
                )
                return None
            code = _error_code(exc)
            if code in {"403", "AccessDenied"}:
                hint = (
                    " This is a requester-pays dataset: check that your AWS "
                    "credentials are valid and that RequestPayer is set."
                    if self._dataset.requester_pays
                    else ""
                )
                raise PermissionError(
                    f"access denied for s3://{bucket}/{product.href}.{hint}"
                ) from exc
            if code == "NoSuchBucket":
                raise RuntimeError(
                    f"bucket not found: s3://{bucket} (check the dataset / bucket name)."
                ) from exc
            raise RuntimeError(
                f"failed to download s3://{bucket}/{product.href}: {exc}"
            ) from exc
        return raw

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Download each product (idempotent) and crop it to the AOI.

        Objects that are absent (e.g. Copernicus DEM / WorldCover tiles
        over ocean) are skipped; if every product is missing, a
        `RuntimeError` is raised so a wholly empty result is not silently
        returned.
        """
        client = self._auth.client()
        raw_dir = self._raw_dir()
        self._warn_cross_region_egress(products)
        written: list[Path] = []
        missing = 0
        for product in tqdm(products, desc="amazon-s3", disable=not products):
            raw = self._download_raw(client, product, raw_dir)
            if raw is None:
                missing += 1
                continue
            written.append(self._localise(raw, product))
        if not written and missing:
            raise RuntimeError(
                f"none of the {missing} planned object(s) exist for this "
                "request; check the bbox / date window / variables."
            )
        return written

    def _warn_cross_region_egress(self, products: list[RemoteProduct]) -> None:
        """Warn once before a large cross-region pull, when sizes are known.

        Sums the object sizes the listing already recorded on the planned
        products (`size_bytes` metadata) and defers to
        `earthlens.base.region.warn_if_egress`: it emits a single warning
        only when the caller runs outside the bucket's region and the
        total exceeds the default threshold. Datasets whose keys are
        planned deterministically (no listing, no known size) contribute
        nothing and never warn. The warning is advisory — the download
        proceeds unchanged.

        `probe=False` keeps this advisory check off the network: the
        caller region is taken from `AWS_REGION` / `AWS_DEFAULT_REGION`
        only, so a warning never blocks the first download on the
        instance-metadata probe chain (matching earthdata's re-point).

        Args:
            products: The planned products about to be fetched.
        """
        known = [
            product.metadata["size_bytes"]
            for product in products
            if "size_bytes" in product.metadata
        ]
        if known:
            warn_if_egress(self._dataset.region, size_bytes=sum(known), probe=False)

    def _localise(self, raw: Path, product: RemoteProduct) -> Path:
        """Crop a downloaded granule to the AOI and write it.

        COG granules carry a CRS and are cropped directly (per-file
        UTM / geostationary COGs are reprojected to WGS84 first). ERA5-style
        regular-grid NetCDF granules carry no GDAL-readable SRS, so they are
        rebuilt into a WGS84 raster (time as bands) before cropping.
        Geostationary NetCDF (GOES) is warped to WGS84 from its GDAL
        subdataset, which carries the CF `+proj=geos` grid-mapping.

        Args:
            raw: The downloaded source file.
            product: The `RemoteProduct` that produced `raw`.

        Returns:
            The path of the cropped output file.
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        west, south, east, north = self._bbox()
        if self._dataset.format == "netcdf":
            if self._dataset.crs is None:
                # Geostationary NetCDF (GOES): warp to WGS84 from the GDAL
                # subdataset, which carries the CF `+proj=geos` grid-mapping.
                data = self._geostationary_to_wgs84(raw, product)
            else:
                data = self._netcdf_to_wgs84_raster(raw, product)
        else:
            data = PyramidsDataset.read_file(str(raw))
            if self._dataset.crs is None:
                data = data.to_crs(4326)
        # touch=False clips to the bbox extent; the default touch=True keeps
        # every cell that touches the mask and leaves the extent uncropped.
        # A polygon aoi= masks to the exact shape (see crop_to_aoi).
        data = crop_to_aoi(
            data, self.space, bbox=[west, south, east, north], touch=False
        )

        # A rebuilt NetCDF is a raster (time as bands), so NetCDF datasets
        # also write a GeoTIFF.
        as_geotiff = self._output_format == "geotiff" or self._dataset.format in (
            "cog",
            "netcdf",
        )
        out_ext = ".tif" if as_geotiff else ".nc"
        out_path = self.path / f"{_safe_name(product.id)}{out_ext}"
        data.to_file(str(out_path))
        return out_path

    def _netcdf_to_wgs84_raster(self, raw: Path, product: RemoteProduct):
        """Rebuild a regular-grid NetCDF variable as a WGS84 pyramids raster.

        Reads the granule's data variable as an array + geotransform and
        builds a `Dataset` tagged EPSG:4326 — NetCDF cubes do not expose a
        source SRS the crop warp can read, so cropping the cube directly
        fails. Longitudes in the 0-360 convention are wrapped to -180..180.
        """
        import numpy as np
        from pyramids.dataset import Dataset as PyramidsDataset
        from pyramids.netcdf import NetCDF

        nc = NetCDF.read_file(str(raw))
        cube = nc.get_variable(self._nc_variable_name(nc, product))
        arr = np.asarray(cube.read_array())
        geo = tuple(cube.geotransform)
        if self._dataset.lon_convention == "0-360":
            arr, geo = _wrap_longitude_0_360(arr, geo)
        return PyramidsDataset.create_from_array(arr=arr, geo=geo, epsg=4326)

    def _geostationary_to_wgs84(self, raw: Path, product: RemoteProduct):
        """Warp a geostationary NetCDF variable (e.g. GOES ABI) to WGS84.

        The variable is opened through GDAL's NetCDF **subdataset**
        (`NETCDF:"<file>":<variable>`) rather than the multidimensional
        `pyramids.netcdf.NetCDF` class. The subdataset exposes the CF
        `goes_imager_projection` grid-mapping as a `+proj=geos` spatial
        reference over a projected-metre geotransform, so `to_crs(4326)`
        warps the scan-angle grid correctly and cheaply.

        The multidimensional path must not be used here. On real GOES ABI
        granules (observed with pyramids 0.45), `NetCDF.read_file` →
        `get_variable` → `read_array` returned the variable's array flipped
        along `y` relative to its north-up geotransform, so the reprojected
        raster kept a correct header while its pixels were mirrored about
        the grid's horizontal centre line — an AOI crop silently returned
        radiances from the wrong latitude. The subdataset read is also ~100x
        faster (a full-disk `read_array()` takes minutes, sub-second here).
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        variable = self._resolve_nc_variable(raw, product)
        cube = PyramidsDataset.read_file(f'NETCDF:"{raw}":{variable}')
        return cube.to_crs(4326)

    def _nc_variable_name(self, nc: Any, product: RemoteProduct) -> str:
        """Resolve the in-file NetCDF data-variable name for `product`.

        Prefers the catalog row's pinned `nc_variable`; otherwise picks the
        variable with the most dimensions (the gridded data variable rather
        than a 1-D auxiliary like ERA5's `utc_date` or GOES's `band_id`).
        """
        names = list(nc.variable_names)
        if len(names) == 1:
            return names[0]
        variable = self._variable_for_native(product.metadata.get("variable"))
        if variable and variable.nc_variable and variable.nc_variable in names:
            return variable.nc_variable

        def _rank(name: str) -> tuple[int, int]:
            # Rank by dimensionality, then de-prioritise known auxiliary
            # variables (quality flags, coordinate bounds, date helpers) so a
            # 2-D `DQF` never outranks a 2-D `CMI` on a tie.
            auxiliary = name in {"DQF"} or name.endswith(("_bounds", "_date", "_id"))
            try:
                ndim = len(nc.get_variable(name).shape)
            except Exception:  # noqa: BLE001
                # pyramids raises (e.g. RuntimeError "Invalid iXDim/iYDim") for a
                # 1-D auxiliary it cannot read as a grid; rank it lowest.
                ndim = 0
            return (ndim, 0 if auxiliary else 1)

        return max(names, key=_rank)

    def _resolve_nc_variable(self, raw: Path, product: RemoteProduct) -> str:
        """In-file NetCDF variable for `product`, opening the granule only if unpinned.

        Uses the catalog's pinned `nc_variable` when present (no I/O); otherwise
        reads the granule and falls back to `_nc_variable_name`.
        """
        variable = self._variable_for_native(product.metadata.get("variable"))
        if variable and variable.nc_variable:
            return variable.nc_variable
        from pyramids.netcdf import NetCDF

        return self._nc_variable_name(NetCDF.read_file(str(raw)), product)

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
            aggregate: Optional `AggregationConfig`. Supported for NetCDF
                datasets (currently ERA5) — each raw granule is run through
                `aggregate_netcdf` to emit per-window GeoTIFFs. Rejected for
                COG datasets.

        Returns:
            The cropped output paths, or — when `aggregate` is set — the
            per-window aggregated GeoTIFF paths.

        Raises:
            NotImplementedError: If `aggregate` is given for a COG dataset.
        """
        if aggregate is not None and self._dataset.format != "netcdf":
            raise NotImplementedError(
                "aggregate= is only supported for NetCDF datasets "
                "(e.g. era5); this dataset is stored as COG."
            )
        products = self._search()
        if aggregate is not None:
            return self._aggregate(products, aggregate)
        return self._fetch(products)

    def _aggregate(self, products: list[RemoteProduct], aggregate: Any) -> list[Path]:
        """Window-aggregate the raw NetCDF granules, mirroring the ECMWF path.

        Aggregation reads the granule's time axis, so it runs on the raw
        NetCDF (not the cropped GeoTIFF). Per-window GeoTIFFs are written to
        `aggregate.out_dir` or `<path>/aggregated`. A granule that fails to
        aggregate is logged and skipped (ECMWF behaviour).

        Args:
            products: The planned products from `_search`.
            aggregate: An `earthlens.aggregate.AggregationConfig`.

        Returns:
            The per-window GeoTIFF paths.
        """
        from earthlens.aggregate import aggregate_netcdf

        client = self._auth.client()
        raw_dir = self._raw_dir()
        out_dir = (
            Path(aggregate.out_dir) if aggregate.out_dir else self.path / "aggregated"
        )
        config = aggregate.model_copy(update={"out_dir": out_dir})
        outputs: list[Path] = []
        for product in products:
            raw = self._download_raw(client, product, raw_dir)
            if raw is None:
                continue
            native = product.metadata.get("variable")
            # Resolve the *in-file* variable name (e.g. ERA5 `128_167_2t` ->
            # `VAR_2T`); the native token is not the NetCDF variable name. Prefer
            # the catalog's pinned `nc_variable` so the granule is not re-opened
            # just to read it (aggregate_netcdf opens it anyway).
            nc_variable = self._resolve_nc_variable(raw, product)
            var_info = _AggregationVariable(
                nc_variable=nc_variable,
                cds_variable=native or "",
                is_flux=False,
            )
            try:
                windows = aggregate_netcdf(raw, var_info, config)
            except Exception as exc:  # noqa: BLE001 - log + continue like ECMWF
                logger.error(f"aggregation failed for {raw}: {exc}")
                continue
            outputs.extend(w[2] for w in windows if w[2] is not None)
        return outputs

    def _variable_for_native(self, native: str | None):
        """Return the dataset `Variable` whose native token matches, or `None`."""
        if native is None:
            return None
        for variable in self._dataset.variables.values():
            if variable.native == native:
                return variable
        return None


def _wrap_longitude_0_360(arr, geo):
    """Roll a global 0-360-longitude array + geotransform to -180..180.

    Assumes a global longitude span (the ERA5 grid): the second half of
    the columns (>= 180 degrees) moves to the front as the negative
    longitudes, and the geotransform origin shifts west by 180 degrees.

    Args:
        arr: Array whose last axis is longitude (`(..., rows, cols)`).
        geo: The GDAL 6-tuple geotransform for `arr`.

    Returns:
        The rolled `(array, geotransform)` pair in the -180..180 convention.
    """
    import numpy as np

    cols = arr.shape[-1]
    half = cols // 2
    rolled = np.concatenate([arr[..., half:], arr[..., :half]], axis=-1)
    new_geo = (geo[0] - 180.0, geo[1], geo[2], geo[3], geo[4], geo[5])
    return rolled, new_geo


def _error_code(exc: BaseException) -> str:
    """Return the S3 error code from a botocore exception, or `""`."""
    response = getattr(exc, "response", None)
    return (response or {}).get("Error", {}).get("Code", "")


def _is_missing_object(exc: BaseException) -> bool:
    """True only for a genuinely-absent object (404 / NoSuchKey).

    Auth (`403` / `AccessDenied`) and bucket (`NoSuchBucket`) errors are
    deliberately excluded — the caller classifies those separately so a
    credential / billing / typo failure is never reported as "no data".
    """
    return _error_code(exc) in {"404", "NoSuchKey"} or "Not Found" in str(exc)
