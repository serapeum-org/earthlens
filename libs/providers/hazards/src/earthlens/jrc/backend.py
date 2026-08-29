"""JRC hazard backend — `JRC(AbstractDataSource)` (EFHM + sea-level forecasts).

`JRC` is one backend for the JRC / Copernicus-EMS hazard products, selected by
dataset and dispatched on the catalog row's `kind` (the `ecmwf`-endpoint /
`RiskIndicators` pattern):

* `flood_hazard_raster` — the European Flood Hazard Map (EFHM): one whole-Europe
  GeoTIFF of river-flood water depth per return period, cropped to the AOI via a
  lazy `/vsicurl` windowed read. Static; the request axis is `return_periods`.
* `sea_level_gridded` — the probabilistic Total Water Level (TWL) forecast cubes
  (medium-term / subseasonal), global 0.25 deg NetCDF-4 read via
  `pyramids.netcdf.NetCDF`. The request axis is a forecast `reference_time`
  (default `"latest"`) plus a bbox and a `field` (default `TWL75`); each cycle is
  resolved by walking the jeodpp autoindex, gated on the `endFls` sentinel. The
  variables arrive index-space over `/vsicurl`, so the backend reconstructs the
  CF affine from the grid shape (interim until pyramids#1071).
* `sea_level_coastal` — the subseasonal global per-country coastal summary CSV,
  returned as a `pandas.DataFrame`.

`OUTPUT_KIND` is set per instance from the resolved row's `kind` (raster for the
two gridded kinds, tabular for the coastal one); the facade reads it to pick the
return shape and to reject `aggregate=` (the products carry no reducible time
axis). All products are public + CC-BY-4.0, so there is no auth and no
`LicenseWarning`. No `xarray` — raster read/crop is pyramids'.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    aoi_tag,
    sidecar_is_fresh,
    write_sidecar,
)
from earthlens.base.spatial import (
    bbox_overlaps,
    crop_to_aoi,
    ensure_no_data,
    vsicurl_config,
    widen_degenerate_bbox,
    windowed_bbox_crop,
)
from earthlens.jrc import _helpers
from earthlens.jrc._helpers import efhm_url
from earthlens.jrc.catalog import Catalog, Dataset

if TYPE_CHECKING:
    import pandas as pd


class JRC(AbstractDataSource):
    """JRC hazard backend (EFHM raster + sea-level TWL forecasts).

    One class serves every JRC dataset; `__init__` resolves the dataset, copies
    its `kind` onto `self.OUTPUT_KIND`, and `_search` / `_fetch` dispatch on that
    kind. EFHM is unchanged (return-period GeoTIFF windowed crop); the sea-level
    kinds resolve a forecast cycle from the jeodpp autoindex and either crop the
    gridded NetCDF field or parse the coastal-summary CSV.

    Attributes:
        OUTPUT_KIND: Set per instance in `__init__` from the resolved row's
            `kind` — `"raster"` for `flood_hazard_raster` / `sea_level_gridded`
            (returns `list[Path]`), `"tabular"` for `sea_level_coastal`
            (returns a `pandas.DataFrame`).

    Examples:
        - EFHM (marked `+SKIP` — it hits the live JRC directory):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="efhm",
            ...     lat_lim=[51.8, 52.0],
            ...     lon_lim=[4.8, 5.0],
            ...     return_periods=[100],
            ...     path="efhm_out",
            ... ).download()

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Maps a catalog row's `kind` to the instance `OUTPUT_KIND`.
    _KIND_TO_OUTPUT: dict[str, OutputKind] = {
        "flood_hazard_raster": "raster",
        "sea_level_gridded": "raster",
        "sea_level_coastal": "tabular",
    }

    AGGREGATE_REFUSAL_REASON = "the JRC hazard products are static per-return-period depth grids or probabilistic forecast cubes with no reducible time axis, so there is nothing to reduce. Call download() without aggregate="

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: The EFHM is static and a forecast cycle is picked by `reference_time`, so
    #: a missing `start` / `end` is legal for every JRC dataset.
    REQUIRES_TIME_WINDOW = False

    def __init__(
        self,
        start: str = "",
        end: str = "",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        return_periods: list[int | str] | int | str | None = None,
        temporal_resolution: str = "static",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        dataset: str | None = None,
        product: str | None = None,
        representation: str | None = None,
        reference_time: str | None = "latest",
        field: str | None = None,
        catalog: Catalog | None = None,
    ):
        """Initialise a JRC backend instance for the resolved dataset.

        Args:
            start: Accepted for facade parity; ignored (products are static /
                cycle-selected).
            end: Accepted for facade parity; ignored.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes. Required for
                the raster kinds; defaulted to global for the coastal kind.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes.
            return_periods: EFHM only — one return period or a list, in years.
            temporal_resolution: Advisory label only.
            path: Output directory for the written raster(s).
            fmt: Accepted for facade parity; unused.
            dataset: Which JRC dataset — a catalog id (`"efhm"`,
                `"sea_level_medium_term"`, …), the family selector `"sea_level"`
                (paired with `product` / `representation`), or `None` for EFHM.
            product: Sea-level family — `"medium_term"` | `"subseasonal"`.
            representation: Sea-level family — `"gridded"` (default) |
                `"coastal"` (subseasonal only).
            reference_time: Sea-level — `"latest"` (default) or an explicit cycle
                (`"2026-08-26T12"`).
            field: Sea-level gridded — the variable to crop (defaults to the
                row's `default_field`, `"TWL75"`).
            catalog: Optional pre-built `Catalog` (tests inject a faked one).

        Raises:
            ValueError: If the dataset / product / representation combination is
                invalid, or a required bounding box is missing.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset: Dataset = self._catalog.get(
            self._resolve_dataset_id(dataset, product, representation)
        )
        self.OUTPUT_KIND = self._KIND_TO_OUTPUT.get(self._dataset.kind, "raster")
        kind = self._dataset.kind

        self._return_periods: list[int] = []
        self._reference_time = reference_time
        self._field = ""
        variables: list[str]

        if kind == "flood_hazard_raster":
            if lat_lim is None or lon_lim is None:
                raise ValueError(
                    "JRC EFHM requires a bounding box (lat_lim=[s, n], "
                    "lon_lim=[w, e]) — a hazard-map subset has no default extent."
                )
            self._return_periods = self._resolve_return_periods(return_periods)
            variables = [self._dataset.band]
        elif kind == "sea_level_gridded":
            if lat_lim is None or lon_lim is None:
                raise ValueError(
                    "JRC sea-level gridded forecasts require a bounding box "
                    "(lat_lim=[s, n], lon_lim=[w, e])."
                )
            self._field = field or self._dataset.default_field or "TWL75"
            variables = [self._field]
        else:  # sea_level_coastal — global, no AOI
            lat_lim = lat_lim if lat_lim is not None else [-90.0, 90.0]
            lon_lim = lon_lim if lon_lim is not None else [-180.0, 180.0]
            variables = [self._dataset.id]

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

    def _resolve_dataset_id(
        self, dataset: str | None, product: str | None, representation: str | None
    ) -> str:
        """Resolve the request selectors to a single catalog dataset id.

        Args:
            dataset: A catalog id, the family selector `"sea_level"`, or `None`.
            product: `"medium_term"` | `"subseasonal"` (sea-level family).
            representation: `"gridded"` | `"coastal"` (sea-level family).

        Returns:
            str: The resolved catalog dataset id.

        Raises:
            ValueError: If the combination is unknown or invalid.
        """
        key = (dataset or "").strip().lower()
        if key in self._catalog.datasets and key != "sea_level":
            return key
        if key in ("", "efhm", "flood", "jrc-flood"):
            return "efhm"
        if key == "sea_level":
            rep = (representation or "gridded").strip().lower()
            prod = (product or "medium_term").strip().lower()
            if rep == "coastal":
                if prod != "subseasonal":
                    raise ValueError(
                        "representation='coastal' is only available for "
                        "product='subseasonal'."
                    )
                return "sea_level_subseasonal_coastal"
            if rep != "gridded":
                raise ValueError(
                    f"representation must be 'gridded' or 'coastal', got "
                    f"{representation!r}."
                )
            if prod not in ("medium_term", "subseasonal"):
                raise ValueError(
                    f"product must be 'medium_term' or 'subseasonal', got "
                    f"{product!r}."
                )
            return f"sea_level_{prod}"
        raise ValueError(
            f"unknown JRC dataset {dataset!r}; available: "
            f"{sorted(self._catalog.datasets)} (or dataset='sea_level' with "
            "product= / representation=)."
        )

    def _resolve_return_periods(
        self, return_periods: list[int | str] | int | str | None
    ) -> list[int]:
        """Normalise + validate the requested return periods against the catalog.

        Args:
            return_periods: The raw request value (int, string, or list).

        Returns:
            list[int]: Sorted, de-duplicated return periods to fetch.

        Raises:
            ValueError: If a value is unparseable or is not a published return
                period.
        """
        available = self._dataset.return_periods
        if return_periods is None:
            requested_raw: list[int | str] = [100]
        elif isinstance(return_periods, (list, tuple)):
            requested_raw = list(return_periods)
        else:
            requested_raw = [return_periods]

        resolved = [self._parse_rp(value) for value in requested_raw]
        unknown = [rp for rp in resolved if rp not in available]
        if unknown:
            raise ValueError(
                f"return period(s) {unknown} are not published for the EFHM; "
                f"available: {available}."
            )
        return sorted(set(resolved))

    @staticmethod
    def _parse_rp(value: int | str) -> int:
        """Parse one return-period token to an int (`100`, `"100"`, `"RP100"`).

        Args:
            value: The raw return-period value.

        Returns:
            int: The integer return period in years.

        Raises:
            ValueError: If `value` is not an int or an `RP`-prefixed / bare
                integer string.
        """
        if isinstance(value, int):
            return value
        text = str(value).strip().upper()
        if text.startswith("RP"):
            text = text[2:]
        try:
            return int(text)
        except ValueError:
            raise ValueError(
                f"could not parse return period {value!r} (expected e.g. 100, "
                "'100', or 'RP100')."
            ) from None

    def _initialize(self):
        """No-op initialiser — every JRC dataset is public + anonymous.

        Returns:
            None: The parent binds no `self.client`.
        """
        return None

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Return a degenerate (timeless) extent — the request is not a scan.

        The EFHM is static and a forecast cycle is picked by `reference_time`, so
        there is no `start` / `end` window to validate.

        Args:
            start: Ignored.
            end: Ignored.
            temporal_resolution: Recorded as the resolution label.
            fmt: Ignored.

        Returns:
            TemporalExtent: A frozen model with `None` bounds and an empty date
                index.
        """
        return self._static_extent(resolution=temporal_resolution or "static")

    @property
    def _bbox(self) -> tuple[float, float, float, float]:
        """The AOI as `(west, south, east, north)` in degrees."""
        return (self.space.west, self.space.south, self.space.east, self.space.north)

    def _bbox_overlaps(self, source: Any) -> bool:
        """Whether the AOI overlaps the source raster's geographic extent."""
        return bbox_overlaps(source, self._bbox)

    def _is_cached(self, target: Path) -> bool:
        """Whether `target` already holds this exact AOI (AOI-aware skip).

        The output filename encodes the return period / cycle / field but not the
        AOI, so a `<target>.aoi` sidecar records the AOI the file was written for;
        the skip only fires when it matches and `force` is off.

        Args:
            target: The candidate output path.

        Returns:
            bool: `True` when a matching cached output exists and may be reused.
        """
        return not getattr(self, "_force", False) and sidecar_is_fresh(
            target, aoi_tag(self.space)
        )

    def download(
        self,
        progress_bar: bool = True,
        *,
        force: bool = False,
    ) -> list[Path] | pd.DataFrame:
        """Fetch the resolved dataset's subset(s).

        Args:
            progress_bar: Accepted for signature parity.
            force: Re-fetch even when a complete output already exists.

        Returns:
            list[pathlib.Path]: One cropped GeoTIFF per return period (EFHM) or
                per gridded cycle. For the coastal kind, a `pandas.DataFrame` of
                the global per-country summary instead.

        Raises:
            ValueError: If the AOI is outside coverage, or a requested cycle is
                missing / not yet complete.
        """
        self._force = force
        products = self._search()
        results = self._fetch(products)
        if self._dataset.kind == "sea_level_coastal":
            return results[0]
        return results

    def _search(self) -> list[RemoteProduct]:
        """Resolve the request to a download plan (dispatched on `kind`).

        Returns:
            list[RemoteProduct]: One product per return period (EFHM), or the
                single resolved forecast cycle (sea-level). Sea-level resolution
                walks the jeodpp autoindex (network).
        """
        kind = self._dataset.kind
        if kind == "flood_hazard_raster":
            return [
                RemoteProduct(
                    id=f"efhm_RP{rp}",
                    metadata={
                        "rp": rp,
                        "url": efhm_url(
                            rp,
                            base_url=self._dataset.base_url,
                            template=self._dataset.filename_template,
                        ),
                    },
                )
                for rp in self._return_periods
            ]

        cycle_url, cycle_id = _helpers.resolve_cycle(
            self._dataset.base_url,
            self._dataset.product,
            self._dataset.cycle_path_template,
            self._reference_time,
            self._dataset.endfls_marker,
            http_text=_helpers._http_text,
        )
        if kind == "sea_level_gridded":
            name = _helpers.find_cycle_file(
                cycle_url, self._dataset.gridded_glob, http_text=_helpers._http_text
            )
            return [
                RemoteProduct(
                    id=f"{self._dataset.id}_{cycle_id}_{self._field}",
                    metadata={"url": f"/vsicurl/{cycle_url}{name}", "cycle": cycle_id},
                )
            ]
        name = _helpers.find_cycle_file(
            cycle_url, self._dataset.coastal_glob, http_text=_helpers._http_text
        )
        return [
            RemoteProduct(
                id=f"{self._dataset.id}_{cycle_id}",
                metadata={"url": f"{cycle_url}{name}", "cycle": cycle_id},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list[Any]:
        """Realise each product (dispatched on `kind`).

        Args:
            products: The plan from `_search`.

        Returns:
            list: Written paths (raster kinds) or a one-element list holding the
                coastal `DataFrame`.
        """
        kind = self._dataset.kind
        if kind == "flood_hazard_raster":
            return [self._fetch_efhm_one(product) for product in products]
        if kind == "sea_level_gridded":
            return [self._fetch_gridded_one(product) for product in products]
        return [self._fetch_coastal(product) for product in products]

    def _fetch_efhm_one(self, product: RemoteProduct) -> Path:
        """Windowed-read + crop one return-period GeoTIFF to the AOI.

        Args:
            product: The `RemoteProduct` whose `metadata` carries `rp` + `url`.

        Returns:
            pathlib.Path: The written GeoTIFF at `<path>/efhm_RP{rp}.tif`.

        Raises:
            ValueError: If the AOI does not overlap the EFHM coverage.
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        from earthlens.base import close_quietly

        rp = product.metadata["rp"]
        url = product.metadata["url"]
        target = Path(self.path) / f"{product.id}.tif"
        if self._is_cached(target):
            logger.info(f"JRC: {target.name} already holds this AOI; skipping.")
            return target

        with vsicurl_config():
            source = PyramidsDataset.read_file(url)
            try:
                if not self._bbox_overlaps(source):
                    raise ValueError(
                        f"the AOI {self._bbox} is outside the EFHM's Europe / "
                        f"Mediterranean coverage; no RP{rp} data to write."
                    )
                logger.info(
                    f"JRC EFHM RP{rp}: windowed /vsicurl crop of {self._bbox}"
                )
                geo = source.geotransform
                bbox = widen_degenerate_bbox(self._bbox, geo[1], geo[5])
                windowed = windowed_bbox_crop(source, bbox, epsg=4326)
            finally:
                close_quietly(source)

        try:
            windowed = ensure_no_data(windowed, self._dataset.nodata)
            cropped = crop_to_aoi(
                windowed,
                self.space,
                bbox=[
                    self.space.west,
                    self.space.south,
                    self.space.east,
                    self.space.north,
                ],
                touch=False,
            )
            staged = target.with_name(f"{target.stem}.part{target.suffix}")
            try:
                cropped.to_file(str(staged))
                close_quietly(cropped)
                staged.replace(target)
            except BaseException:
                close_quietly(cropped)
                staged.unlink(missing_ok=True)
                raise
        finally:
            close_quietly(windowed)
        write_sidecar(target, aoi_tag(self.space))
        return target

    def _fetch_gridded_one(self, product: RemoteProduct) -> Path:
        """Windowed-read + crop one sea-level TWL field to the AOI.

        Opens the global NetCDF cube lazily over `/vsicurl` with
        `pyramids.netcdf.NetCDF`, reconstructs the CF affine from the grid shape
        (the variable arrives index-space; interim until pyramids#1071), reads
        only the AOI pixel window across every forecast time step, rebuilds a
        small georeferenced `Dataset`, crops to the exact bbox / polygon, and
        writes one multi-band GeoTIFF (band = forecast time step).

        Args:
            product: The `RemoteProduct` whose `metadata` carries the `/vsicurl`
                URL + cycle id.

        Returns:
            pathlib.Path: The written GeoTIFF at
                `<path>/<dataset>_<cycle>_<field>.tif`.

        Raises:
            ValueError: If the AOI does not overlap the grid.
        """
        import numpy as np
        from pyramids.dataset import Dataset as PyramidsDataset
        from pyramids.netcdf import NetCDF

        from earthlens.base import close_quietly

        url = product.metadata["url"]
        target = Path(self.path) / f"{product.id}.tif"
        if self._is_cached(target):
            logger.info(f"JRC: {target.name} already holds this AOI; skipping.")
            return target

        with vsicurl_config():
            container = NetCDF.read_file(url)
            try:
                variable = container.get_variable(self._field)
                cols, rows = variable.columns, variable.rows
                geo = _helpers.grid_geotransform(cols, rows)
                window = _helpers.pixel_window(geo, self._bbox, cols, rows)
                if window is None:
                    raise ValueError(
                        f"the AOI {self._bbox} is outside the sea-level grid; "
                        f"nothing to write for {self._field!r}."
                    )
                col_off, row_off, win_cols, win_rows = window
                logger.info(
                    f"JRC {self._dataset.id}: windowed /vsicurl read of "
                    f"{self._field!r} {win_cols}x{win_rows} at ({col_off}, {row_off})"
                )
                array = np.asarray(
                    variable.read_array(
                        window=[col_off, row_off, win_cols, win_rows]
                    ),
                    dtype="float32",
                )
                window_geo = _helpers.window_origin(geo, col_off, row_off)
            finally:
                close_quietly(container)

        window_ds = PyramidsDataset.create_from_array(
            array, geo=window_geo, epsg=4326, no_data_value=float("nan")
        )
        try:
            cropped = crop_to_aoi(
                window_ds,
                self.space,
                bbox=[
                    self.space.west,
                    self.space.south,
                    self.space.east,
                    self.space.north,
                ],
                touch=False,
            )
            staged = target.with_name(f"{target.stem}.part{target.suffix}")
            try:
                cropped.to_file(str(staged))
                close_quietly(cropped)
                staged.replace(target)
            except BaseException:
                close_quietly(cropped)
                staged.unlink(missing_ok=True)
                raise
        finally:
            close_quietly(window_ds)
        write_sidecar(target, aoi_tag(self.space))
        return target

    def _fetch_coastal(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch + parse the global coastal-summary CSV to a `DataFrame`.

        Args:
            product: The `RemoteProduct` whose `metadata` carries the CSV URL.

        Returns:
            pandas.DataFrame: The per-country exceedance-probability summary.
        """
        from io import StringIO

        import pandas as pd

        url = product.metadata["url"]
        logger.info(f"JRC {self._dataset.id}: reading coastal summary {url}")
        return pd.read_csv(StringIO(_helpers._http_text(url)))
