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
  grid comes from the cube's own CF `latitude` / `longitude` via pyramids
  (>= 0.58.1).
* `sea_level_coastal` — the subseasonal global per-country coastal summary CSV,
  returned as a `pandas.DataFrame`.

`OUTPUT_KIND` is set per instance from the resolved row's `kind` (raster for the
two gridded kinds, tabular for the coastal one); the facade reads it to pick the
return shape and to reject `aggregate=` (a static hazard map and a single
forecast cycle are neither a calendar series to reduce over). All products are
public + CC-BY-4.0, so there is no auth and no
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
from earthlens.jrc._helpers import _safe_name, efhm_url
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

    #: Guard on the cells a single gridded read may materialise (cols x rows x
    #: forecast steps). A global AOI over a 47-step cube is ~0.5 GB in memory.
    MAX_WINDOW_CELLS: int = 60_000_000

    AGGREGATE_REFUSAL_REASON = (
        "the JRC hazard products are either static per-return-period depth grids "
        "or a single forecast cycle whose bands are lead times, not a calendar "
        "series to reduce over. Call download() without aggregate=, and reduce the "
        "written bands yourself if you need a summary"
    )

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
                (paired with `product`), or `None` for EFHM.
            product: Sea-level family — `"medium_term"` | `"subseasonal"`.
                Selects the gridded cube; the coastal summary is its own dataset
                (`dataset="sea_level_subseasonal_coastal"`, or the
                `coastal-forecast` facade key).
            reference_time: Sea-level — `"latest"` (default) or an explicit cycle
                (`"2026-08-26T12"`).
            field: Sea-level gridded — the variable to crop (defaults to the
                row's `default_field`, `"TWL75"`).
            catalog: Optional pre-built `Catalog`; one is loaded when omitted.

        Raises:
            ValueError: If the dataset / product combination is invalid, or a
                required bounding box is missing.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._dataset: Dataset = self._catalog.get(
            self._resolve_dataset_id(dataset, product)
        )
        kind = self._dataset.kind
        if kind not in self._KIND_TO_OUTPUT:
            raise ValueError(
                f"catalog dataset {self._dataset.id!r} declares an unhandled kind "
                f"{kind!r}; expected one of {sorted(self._KIND_TO_OUTPUT)}."
            )
        self.OUTPUT_KIND = self._KIND_TO_OUTPUT[kind]
        # Only the two raster kinds honour an AOI; declaring this per instance lets
        # the base class warn when a polygon is handed to the global coastal table.
        self.SUPPORTS_POLYGON_AOI = kind != "sea_level_coastal"

        self._warn_cross_kind_arguments(kind, return_periods, field, reference_time)

        self._return_periods: list[int] = []
        self._reference_time = reference_time
        self._field = ""

        if kind == "flood_hazard_raster":
            lat_lim, lon_lim = self._require_bbox(lat_lim, lon_lim, "EFHM")
            self._return_periods = self._resolve_return_periods(return_periods)
            variables = [self._dataset.band]
        elif kind == "sea_level_gridded":
            lat_lim, lon_lim = self._require_bbox(
                lat_lim, lon_lim, "sea-level gridded forecasts"
            )
            self._field = field or self._dataset.default_field or "TWL75"
            variables = [self._field]
        else:  # sea_level_coastal — a global table; the AOI does not apply
            # The facade substitutes a global default when the caller passes no
            # AOI, so only a genuinely narrowed box is worth reporting.
            narrowed = (lat_lim is not None and tuple(lat_lim) != (-90.0, 90.0)) or (
                lon_lim is not None and tuple(lon_lim) != (-180.0, 180.0)
            )
            if narrowed:
                logger.warning(
                    "JRC: the coastal summary is a global per-country table, so "
                    "lat_lim / lon_lim are ignored; filter the returned frame."
                )
            lat_lim = lat_lim if lat_lim is not None else [-90.0, 90.0]
            lon_lim = lon_lim if lon_lim is not None else [-180.0, 180.0]
            variables = ["coastal_summary"]

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

    @staticmethod
    def _resolve_sea_level_id(product: str | None) -> str:
        """Resolve the sea-level family selector to one gridded dataset id.

        The coastal summary is a dataset in its own right rather than a mode of
        the gridded ones, so it is selected by id (or by the `coastal-forecast`
        facade key) instead of a second selector.

        Args:
            product: `"medium_term"` (default) | `"subseasonal"`.

        Returns:
            str: The resolved catalog dataset id.

        Raises:
            ValueError: If the product is not published.
        """
        prod = (product or "medium_term").strip().lower()
        if prod not in ("medium_term", "subseasonal"):
            raise ValueError(
                f"product must be 'medium_term' or 'subseasonal', got {product!r}."
            )
        return f"sea_level_{prod}"

    @staticmethod
    def _warn_cross_kind_arguments(kind, return_periods, field, reference_time) -> None:
        """Warn when an argument that belongs to another kind was passed.

        The selectors are shared by every JRC dataset, so a `return_periods=` sent
        to a forecast (or a `field=` / non-default `reference_time=` sent to the
        static EFHM) would otherwise be dropped without a word.

        Args:
            kind: The resolved dataset's kind.
            return_periods: The EFHM-only return-period selector, if given.
            field: The gridded-only field selector, if given.
            reference_time: The sea-level-only cycle selector, if given.
        """
        if kind == "flood_hazard_raster":
            unused = [
                name
                for name, value in (
                    ("field", field),
                    ("reference_time", reference_time),
                )
                if not (
                    value is None
                    or (name == "reference_time" and _helpers._is_latest(value))
                )
            ]
        else:
            unused = ["return_periods"] if return_periods is not None else []
            if kind == "sea_level_coastal" and field is not None:
                unused.append("field")
        if unused:
            logger.warning(
                f"JRC: {', '.join(unused)} does not apply to a "
                f"{kind!r} dataset and is ignored."
            )

    @staticmethod
    def _require_bbox(
        lat_lim: list[float] | None, lon_lim: list[float] | None, what: str
    ) -> tuple[list[float], list[float]]:
        """Reject a request whose kind needs an AOI but was given none.

        Returns the pair so the caller binds non-optional bounds (the type
        checker cannot narrow `None` away across a helper that returns nothing).

        Args:
            lat_lim: The requested latitudes, or `None`.
            lon_lim: The requested longitudes, or `None`.
            what: The product name to quote in the message.

        Returns:
            tuple[list[float], list[float]]: The validated `(lat_lim, lon_lim)`.

        Raises:
            ValueError: If either bound is missing.
        """
        if lat_lim is None or lon_lim is None:
            raise ValueError(
                f"JRC {what} require a bounding box (lat_lim=[s, n], "
                "lon_lim=[w, e]) — a subset has no default extent."
            )
        return lat_lim, lon_lim

    def _resolve_dataset_id(self, dataset: str | None, product: str | None) -> str:
        """Resolve the request selectors to a single catalog dataset id.

        Args:
            dataset: A catalog id, the family selector `"sea_level"`, or `None`.
            product: `"medium_term"` | `"subseasonal"` (sea-level family).

        Returns:
            str: The resolved catalog dataset id.

        Raises:
            ValueError: If the combination is unknown or invalid.
        """
        key = (dataset or "").strip().lower()
        if key in self._catalog.datasets and key != "sea_level":
            # An explicit catalog id already pins the row, so the family
            # selectors cannot apply — say so rather than dropping them.
            ignored = ["product"] if product is not None else []
            if ignored:
                logger.warning(
                    f"JRC: dataset={dataset!r} names a dataset directly, so "
                    f"{', '.join(ignored)} ignored."
                )
            return key
        if key in ("", "flood", "jrc-flood"):
            return "efhm"
        if key == "sea_level":
            return self._resolve_sea_level_id(product)
        raise ValueError(
            f"unknown JRC dataset {dataset!r}; available: "
            f"{sorted(self._catalog.datasets)} (or dataset='sea_level' with "
            "product=)."
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
            NotImplementedError: If a non-`None` `aggregate=` is passed (the
                products carry no reducible calendar axis).
            requests.HTTPError: If the JRC server fails while resolving a cycle.
        """
        # Stashed for the fetch helpers to read. One backend instance is one
        # request, so this is not shared across concurrent downloads.
        self._force = force
        # NOTE: the cache check lives in the per-product fetch, after _search has
        # resolved the cycle. Resolving is a handful of small listings against a
        # pooled session, and the cycle id is part of the output name, so it has
        # to be known before a cached file can be identified.
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
        if kind not in self._KIND_TO_OUTPUT:
            # Guard before any network work, mirroring `_fetch`.
            raise ValueError(f"unhandled JRC dataset kind {kind!r}.")
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
        )
        if kind == "sea_level_gridded":
            name = _helpers.find_cycle_file(cycle_url, self._dataset.gridded_glob)
            return [
                RemoteProduct(
                    id=f"{self._dataset.id}_{cycle_id}_{_safe_name(self._field)}",
                    metadata={"url": f"/vsicurl/{cycle_url}{name}", "cycle": cycle_id},
                )
            ]
        name = _helpers.find_cycle_file(cycle_url, self._dataset.coastal_glob)
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
        if kind == "sea_level_coastal":
            return [self._fetch_coastal(product) for product in products]
        raise ValueError(f"unhandled JRC dataset kind {kind!r}.")

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

        # Tune the /vsicurl read (readdir-suppression + retry/timeout) for the
        # duration of the open + windowed crop; a plain read_file installs none.
        with vsicurl_config():
            source = PyramidsDataset.read_file(url)
            try:
                if not self._bbox_overlaps(source):
                    raise ValueError(
                        f"the AOI {self._bbox} is outside the EFHM's Europe / "
                        f"Mediterranean coverage; no RP{rp} data to write."
                    )
                logger.info(f"JRC EFHM RP{rp}: windowed /vsicurl crop of {self._bbox}")
                # A point / cell-edge AOI (min == max on an axis) is widened to
                # one source pixel so crop(bbox=)'s fast path yields a 1x1 window
                # rather than raising on the zero-width box.
                geo = source.geotransform
                bbox = widen_degenerate_bbox(self._bbox, geo[1], geo[5])
                # The windowed fast path reads only the AOI pixel window from the
                # ~23 GB source; nodata / CRS / grid are carried onto the crop.
                windowed = windowed_bbox_crop(source, bbox, epsg=4326)
            finally:
                close_quietly(source)

        try:
            # crop carries the source's own no-data through; when the source
            # declares none, fall back to the catalog value so the output stays
            # flagged and a polygon `aoi=` can trim exactly.
            nodata = (
                self._dataset.nodata if self._dataset.nodata is not None else -9999.0
            )
            windowed = ensure_no_data(windowed, nodata)
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
        `pyramids.netcdf.NetCDF`, which derives the CF affine from the cube's
        `latitude` / `longitude` coordinates, reads only the AOI pixel window
        across every forecast time step, rebuilds a small georeferenced
        `Dataset`, crops to the exact bbox / polygon, and writes one multi-band
        GeoTIFF (band = forecast time step).

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
            variable = None
            try:
                variable = container.get_variable(self._field)
                # Not every variable in the cube is on the lat/lon grid (the
                # `*coast*` fields are indexed by coastal point, and the coordinate
                # variables are 1-D), and those come back as a bare MDArray. Fail
                # with a message naming the gridded alternatives rather than an
                # opaque AttributeError deep in the read.
                if not hasattr(variable, "columns") or not hasattr(variable, "rows"):
                    raise ValueError(
                        f"field {self._field!r} is not a gridded field of "
                        f"{self._dataset.id!r} (it is not on the lat/lon grid). "
                        f"Pass a gridded field such as "
                        f"{self._dataset.default_field or 'TWL75'!r}."
                    )
                cols, rows = variable.columns, variable.rows
                # pyramids >= 0.58.1 derives the affine from the cube's own CF
                # latitude/longitude (serapeum-org/pyramids#1071), so the grid is
                # read from the file rather than assumed from its shape.
                geo = variable.geotransform
                _helpers.require_geographic_affine(geo, cols, rows, self._dataset.id)
                # Widen a point / cell-edge AOI to one pixel so an on-grid point
                # yields a 1x1 window rather than being reported off-grid (matches
                # the EFHM path).
                bbox = widen_degenerate_bbox(self._bbox, geo[1], geo[5])
                window = _helpers.pixel_window(geo, bbox, cols, rows)
                if window is None:
                    raise ValueError(
                        f"the AOI {self._bbox} is outside the sea-level grid; "
                        f"nothing to write for {self._field!r}."
                    )
                col_off, row_off, win_cols, win_rows = window
                # `or 1` here would do exactly what it must not: a container-like
                # variable reports 0 bands, and treating that as 1 would under-count
                # the read by orders of magnitude. A gridded field always reports a
                # positive band count, so anything else is not one.
                steps_hint = getattr(variable, "band_count", None)
                if not steps_hint:
                    raise ValueError(
                        f"{self._dataset.id!r} field {self._field!r} reports "
                        f"{steps_hint!r} bands, so it is not a gridded forecast "
                        "field. Request a gridded field."
                    )
                cells = win_cols * win_rows * steps_hint
                if cells > self.MAX_WINDOW_CELLS:
                    raise ValueError(
                        f"the AOI would materialise {cells:,} cells "
                        f"({win_cols}x{win_rows} over {steps_hint} steps), above the "
                        f"{self.MAX_WINDOW_CELLS:,}-cell guard. Request a smaller bbox."
                    )
                logger.info(
                    f"JRC {self._dataset.id}: windowed /vsicurl read of "
                    f"{self._field!r} {win_cols}x{win_rows} at ({col_off}, {row_off})"
                )
                # `masked=True` so a field that declares a numeric `_FillValue` comes
                # back masked; `filled` then turns both that and the cubes' own NaN
                # gaps into the NaN this writes as no-data.
                raw = variable.read_array(
                    window=[col_off, row_off, win_cols, win_rows], masked=True
                )
                # Cast first: filling an integer array with NaN raises, and the
                # cube's categorical fields (severity flags) are integer-stored.
                array = np.ma.filled(np.ma.asarray(raw).astype("float32"), np.nan)
                window_geo = _helpers.window_origin(geo, col_off, row_off)
                # The cube's time axis becomes the output's band axis, so carry the
                # valid times across or the bands are unidentifiable.
                steps = array.shape[0] if array.ndim == 3 else 1
                band_names = _helpers.band_valid_times(url, steps)
            finally:
                # The variable is a separate pyramids object with its own GDAL
                # handle; closing only the container leaks it (variable first).
                close_quietly(variable)
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
                written_bands = getattr(cropped, "band_count", 0)
                if len(band_names) == written_bands:
                    cropped.band_names = band_names
                else:
                    logger.warning(
                        f"JRC {self._dataset.id}: {len(band_names)} band labels for "
                        f"{written_bands} written bands; leaving them unnamed."
                    )
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
        from io import BytesIO

        import pandas as pd

        url = product.metadata["url"]
        logger.info(f"JRC {self._dataset.id}: reading coastal summary {url}")
        # Read the bytes and decode explicitly: the server omits the charset, so
        # letting requests guess mangles the UTF-8 country names.
        return pd.read_csv(BytesIO(_helpers.http_bytes(url)), encoding="utf-8")
