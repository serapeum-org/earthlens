"""Backend that fetches NOAA National Water Model output from S3.

`NWM(AbstractDataSource)` pulls National Water Model v3.0 output from the
**unsigned** `noaa-nwm-pds` AWS bucket. NWM is NOAA's operational
hydrologic model: it routes the land-surface water budget onto the
NHDPlus v2 river network, producing per-reach streamflow (`chrtout` /
`channel_rt`, indexed by `feature_id` — **not** a lat/lon grid) plus
gridded land-surface states (`ldasout` / `land`).

The request is two-axis. `variables = {product: [variable, ...]}` selects
the products (`{"chrtout": ["streamflow"]}`); the `configuration=` keyword
picks which operational run produced them (`short_range`,
`analysis_assim`, `medium_range`). A configuration runs on UTC `cycles`
and publishes forecast (`fNNN`) or analysis (`tmNN`) `steps`; the backend
crosses cycles x steps x products to enumerate the exact S3 keys.

Two properties shape the backend:

* **Per-product output kind.** `chrtout` is `tabular` (a `feature_id`
  table); `ldasout` is `raster` (a 1 km grid). `OUTPUT_KIND` is set per
  instance from the resolved products, and a request mixing kinds raises
  `ValueError`. The facade rejects `aggregate=` for either.
* **Whole-CONUS download + subset/decode.** An operational NWM file is
  whole-CONUS (~14 MB for `channel_rt`, ~30 MB for `land`). A plain
  request downloads the whole files (boto3, no read). A **subset** — by
  `sites=` (`feature_id` / USGS `gage_id`), a narrower bbox, or a
  `[start, end]` window — and the **retrospective** archive
  (`mode="retrospective"`) are read through pyramids (≥ 0.38.0): the
  feature/lake/node-indexed **tabular** products (`chrtout`, `lakeout`,
  `coastal`) go through `pyramids.netcdf.LabeledDataset` (open anon +
  lazily, select labels/bbox/time, write a tidy `feature_id × time`
  Parquet table); the **gridded** products (`ldasout`, `rtout`,
  `forcing`) go through `pyramids.netcdf.NetCDF.subset` (a windowed bbox
  crop on the file's native Lambert-Conformal-Conic grid, written as
  GeoTIFF). earthlens never imports `xarray` / `zarr` itself — pyramids
  owns the read. The gridded **retrospective** is deferred (the retro
  Zarr does not surface CF time units, so a date window cannot be mapped
  to the integer timesteps `NetCDF.subset` selects by) and raises a
  clear `NotImplementedError`.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, TypeGuard

from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    close_quietly,
    date_windows,
    safe_filename,
)
from earthlens.nwm.catalog import Catalog, NWMConfig, NWMProduct

#: The unsigned AWS bucket holding NWM operational output.
BUCKET = "noaa-nwm-pds"

#: The retrospective (v3.0) Zarr bucket — read (never downloaded whole)
#: through `pyramids.netcdf.LabeledDataset` for the tabular products.
RETRO_BUCKET = "noaa-nwm-retrospective-3-0-pds"

#: Approximate operational retention: `noaa-nwm-pds` keeps a rolling
#: archive (~500+ days as of 2026-05). A window ending before this many
#: days ago auto-routes to the retrospective mode.
OPERATIONAL_RETENTION_DAYS = 500

#: Deferral message for the gridded (raster) **retrospective** path — the
#: gridded `NetCDF.subset` reader selects time by integer index, but the NWM
#: retrospective Zarr does not surface CF time units (`NetCDF.time_stamp` is
#: `None`), so a date window cannot yet be mapped to the right timesteps.
_GRIDDED_RETRO_MESSAGE = (
    "NWM retrospective reads for the gridded products (ldasout, rtout, forcing) "
    "are not yet supported: the pyramids NetCDF.subset reader selects time by "
    "integer index, but the retrospective Zarr does not surface CF time units, "
    "so a [start, end] date window cannot be mapped to timesteps reliably. Use "
    "mode='operational' for the gridded products (an operational sites=/bbox "
    "request is read + cropped), or one of the tabular products (chrtout, "
    "lakeout, coastal) for a retrospective time series."
)


def enumerate_cycles(
    start: dt.datetime, end: dt.datetime, cycles_utc: list[int]
) -> list[dt.datetime]:
    """Enumerate the model cycles in `[start, end]` for the given run hours.

    Walks every calendar day from `start` to `end` inclusive and emits one
    naive-UTC datetime per run hour on that day, ascending.

    Args:
        start: Inclusive start of the cycle-date range (only its date is
            used).
        end: Inclusive end of the cycle-date range.
        cycles_utc: Daily run hours, in `[0, 23]`.

    Returns:
        list[datetime.datetime]: One datetime per `(day, run-hour)`,
            ascending.

    Raises:
        ValueError: If `start` is later than `end`, or a run hour is out
            of `[0, 23]`.

    Examples:
        - Two cycles across one day:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwm.backend import enumerate_cycles
            >>> day = dt.datetime(2026, 1, 1)
            >>> [c.hour for c in enumerate_cycles(day, day, [0, 12])]
            [0, 12]

            ```
    """
    if start.date() > end.date():
        raise ValueError(f"start {start.date()} is after end {end.date()}.")
    bad = [h for h in cycles_utc if not 0 <= h <= 23]
    if bad:
        raise ValueError(f"run hour(s) {bad} are outside [0, 23].")
    out: list[dt.datetime] = []
    for day in date_windows(start.date(), end.date(), "D"):
        for hour in sorted(cycles_utc):
            out.append(dt.datetime(day.year, day.month, day.day, hour))
    return out


def build_key(
    config: NWMConfig, product: NWMProduct, cycle: dt.datetime, step: int, member: int
) -> str:
    """Assemble the S3 object key for one `(config, product, cycle, step)`.

    Mirrors the verified `noaa-nwm-pds` layout: the directory is the
    configuration key (`short_range`, `analysis_assim_hawaii`,
    `short_range_coastal_atlgulf`), with a `_mem{member}` suffix for an
    ensemble; the file name uses the configuration's `family` token and
    the product's `s3_token` (the member rides on the token for an
    ensemble, `channel_rt_1`). The step token is the configuration's
    prefix (`f` forecast / `tm` analysis) zero-padded to its `step_width`
    (`f001`, `tm00`, `f00015`). The domain suffixes the file name.

    Args:
        config: The resolved configuration row.
        product: The resolved product row.
        cycle: The cycle datetime (its date and hour are used).
        step: The forecast / analysis step.
        member: Ensemble member (ignored for deterministic configs).

    Returns:
        str: The bucket-relative S3 key.
    """
    if config.members:
        directory = f"{config.key}_mem{member}"
        token = f"{product.s3_token}_{member}"
    else:
        directory = config.key
        token = product.s3_token
    prefix = "f" if config.step_kind == "forecast" else "tm"
    step_token = f"{prefix}{step:0{config.step_width}d}"
    name = (
        f"nwm.t{cycle.hour:02d}z.{config.family}.{token}."
        f"{step_token}.{config.domain}.nc"
    )
    return f"nwm.{cycle:%Y%m%d}/{directory}/{name}"


class NWM(AbstractDataSource):
    """NOAA National Water Model backend (operational NetCDF output).

    Wraps the unsigned `noaa-nwm-pds` bucket so a user pulls a
    product / configuration / cycle window of NWM output through the same
    `download()` shape every other earthlens backend uses.

    Attributes:
        OUTPUT_KIND: Set per instance from the resolved products —
            `"tabular"` for the feature-id-indexed `chrtout`, `"raster"`
            for the gridded `ldasout`. The facade rejects `aggregate=`
            for both. The class default is `"raster"`.
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = "chrtout is feature-id indexed (not griddable) and a gridded reduce needs a separate gridded reader"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        *,
        configuration: str = "short_range",
        mode: str | None = None,
        member: int = 1,
        cycles: list[int] | None = None,
        steps: list[int] | None = None,
        horizon: int | None = None,
        sites: list[int | str] | None = None,
        region: str = "us-east-1",
        catalog: Catalog | None = None,
    ):
        """Initialise a National Water Model backend instance.

        Args:
            start: Inclusive start of the cycle-date range (parsed with
                `fmt`).
            end: Inclusive end of the cycle-date range.
            variables: Mapping from NWM product key to the variable names
                to pull, e.g. `{"chrtout": ["streamflow"]}`. The MVP
                downloads whole files, so the variable list is validated
                (helpful errors) but every variable in the file is
                fetched. An empty list selects all of the product's
                variables.
            lat_lim: `[lat_min, lat_max]` in degrees. A whole-Earth box
                (`[-90, 90]`) means "no spatial subset"; a narrower box is
                a subset request (read + cropped, see the module
                docstring).
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label (NWM cadence is fixed by
                the configuration).
            path: Output directory for the fetched NetCDF files.
            fmt: `strptime` format for `start` / `end`.
            configuration: The operational configuration key
                (`"short_range"`, `"analysis_assim"`, `"medium_range"`).
            mode: `"operational"` (NetCDF on `noaa-nwm-pds`) or
                `"retrospective"` (the v3.0 Zarr; tabular products only).
                `None` auto-routes by the date window.
            member: Ensemble member (1-based) for an ensemble
                configuration; ignored for deterministic ones.
            cycles: Restrict the run hours fetched (a subset of the
                configuration's `cycles_utc`); defaults to every cycle.
            steps: Explicit steps to fetch; wins over `horizon`.
            horizon: Maximum step; expands from the configuration's
                `first_step` on its `step_cadence_h`.
            sites: Explicit `feature_id`s / USGS `gage_id`s to subset to
                (tabular products only; rejected for a gridded product).
            region: AWS region of the bucket.
            catalog: Optional pre-built :class:`Catalog` (tests inject
                one); defaults to the bundled catalog.

        Raises:
            ValueError: If `variables` is empty, a product is unknown, a
                product is not published under `configuration`, the
                products mix output kinds, or `member` is out of range.
        """
        if not variables:
            raise ValueError(
                "NWM requires a non-empty `variables` mapping of "
                "{product: [variable, ...]}, e.g. {'chrtout': ['streamflow']}."
            )
        self._catalog = catalog if catalog is not None else Catalog()
        self._config_key = configuration
        self._config: NWMConfig = self._catalog.get_config(configuration)
        self._mode_arg = mode
        self._member = member
        self._cycles_arg = cycles
        self._steps_arg = steps
        self._horizon_arg = horizon
        self._sites = sites
        self._region = region
        self._s3_client: Any = None
        self._show_progress = True

        self._products: list[NWMProduct] = []
        self._requested: dict[str, list[str]] = {}
        for product_key, names in variables.items():
            product = self._catalog.get_product(product_key)
            if product_key not in self._config.products:
                raise ValueError(
                    f"product {product_key!r} is not published under "
                    f"configuration {configuration!r}; it carries "
                    f"{self._config.products}."
                )
            unknown = [n for n in names if n not in product.variables]
            if unknown:
                raise ValueError(
                    f"variable(s) {unknown} are not in product {product_key!r}; "
                    f"available: {sorted(product.variables)}."
                )
            self._products.append(product)
            self._requested[product_key] = list(names)

        kinds = {p.output_kind for p in self._products}
        if len(kinds) > 1:
            raise ValueError(
                "all requested NWM products must share one output_kind; got "
                f"{sorted(kinds)} — split the request per kind."
            )
        self.OUTPUT_KIND = kinds.pop()

        if self._config.members:
            if not 1 <= member <= self._config.members:
                raise ValueError(
                    f"member {member} is out of range for configuration "
                    f"{configuration!r} (members 1-{self._config.members})."
                )

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
        self._mode = self._resolve_mode()

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the cycle-date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="raw")

    def _resolve_mode(self) -> str:
        """Resolve operational vs retrospective for this request.

        An explicit `mode=` wins. Otherwise the window auto-routes: a
        window ending within the operational retention
        (:data:`OPERATIONAL_RETENTION_DAYS`) is `operational`, an older
        window is `retrospective`.

        Returns:
            str: `"operational"` or `"retrospective"`.

        Raises:
            ValueError: If an explicit `mode=` is neither value.
        """
        if self._mode_arg is not None:
            if self._mode_arg not in ("operational", "retrospective"):
                raise ValueError(
                    f"mode must be 'operational' or 'retrospective', got "
                    f"{self._mode_arg!r}."
                )
            return self._mode_arg
        cutoff = dt.datetime.now() - dt.timedelta(days=OPERATIONAL_RETENTION_DAYS)
        return "operational" if self.time.end_date >= cutoff else "retrospective"

    def _cycles_for(self) -> list[int]:
        """Resolve the run hours to fetch for the configuration.

        Returns:
            list[int]: The requested run hours, validated against the
                configuration.

        Raises:
            ValueError: When a requested cycle is not one the
                configuration runs.
        """
        if self._cycles_arg is None:
            return list(self._config.cycles_utc)
        unknown = [c for c in self._cycles_arg if c not in self._config.cycles_utc]
        if unknown:
            raise ValueError(
                f"cycle(s) {unknown} are not run by configuration "
                f"{self._config_key!r} {self._config.cycles_utc}."
            )
        return sorted(set(self._cycles_arg))

    def _steps_for(self) -> list[int]:
        """Resolve the forecast / analysis steps to fetch.

        Precedence: an explicit `steps=` list wins; otherwise `horizon=`
        expands from the configuration's `first_step` to the horizon on
        its `step_cadence_h`; otherwise just the `first_step`.

        Returns:
            list[int]: The steps to fetch, ascending.

        Raises:
            ValueError: When a requested step exceeds the configuration's
                horizon.
        """
        if self._steps_arg is not None:
            steps = sorted({int(s) for s in self._steps_arg})
        elif self._horizon_arg is not None:
            steps = list(
                range(
                    self._config.first_step,
                    int(self._horizon_arg) + 1,
                    max(self._config.step_cadence_h, 1),
                )
            )
        else:
            steps = [self._config.first_step]
        too_far = [s for s in steps if s > self._config.horizon_h]
        if too_far:
            raise ValueError(
                f"step(s) {too_far} exceed the {self._config.horizon_h} h horizon "
                f"of configuration {self._config_key!r}."
            )
        return steps

    def _wants_subset(self) -> bool:
        """Return whether the request asks for a subset (needs a read).

        A subset is requested when `sites=` was given or the bbox is
        narrower than whole-Earth. Operational files are whole-CONUS, so a
        subset is read + sliced through the pyramids reader rather than
        downloaded whole.

        Returns:
            bool: `True` when a subset is requested.
        """
        if self._sites is not None:
            return True
        whole_earth = (
            self.space.latitude_min <= -90.0
            and self.space.latitude_max >= 90.0
            and self.space.longitude_min <= -180.0
            and self.space.longitude_max >= 180.0
        )
        return not whole_earth

    def _search(self) -> list[RemoteProduct]:
        """Enumerate one product per `(config, cycle, step, product)`.

        For the operational mode, crosses every in-window cycle with every
        requested step and product, formatting the exact S3 key (no
        re-listing). For the retrospective mode, emits one product per
        requested NWM product carrying the Zarr store URI (read by
        :meth:`_fetch_retrospective`).

        Returns:
            list[RemoteProduct]: One product per item to fetch; each
                carries `href` (the S3 key or Zarr URI) and
                `product` / `cycle` / `step` metadata.
        """
        if self._mode == "retrospective":
            return [
                RemoteProduct(
                    id=f"{p.product}-retro",
                    href=p.retro_zarr,
                    metadata={"product": p.product, "mode": "retrospective"},
                )
                for p in self._products
            ]
        products: list[RemoteProduct] = []
        cycles = enumerate_cycles(
            self.time.start_date, self.time.end_date, self._cycles_for()
        )
        steps = self._steps_for()
        for cycle in cycles:
            for step in steps:
                for product in self._products:
                    key = build_key(self._config, product, cycle, step, self._member)
                    products.append(
                        RemoteProduct(
                            id=f"{self._config_key}.{cycle:%Y%m%d%H}."
                            f"{product.s3_token}.{step}",
                            href=key,
                            metadata={
                                "product": product.product,
                                "cycle": cycle,
                                "step": step,
                                "mode": "operational",
                            },
                        )
                    )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Route the request to the whole-file, subset, or retrospective path.

        * `mode="retrospective"` → read each product's Zarr store through
          the pyramids reader and write a tidy table (tabular products).
        * operational + a `sites=`/bbox subset → download the whole files,
          then read + slice each through the pyramids reader (tabular).
        * operational + no subset → download the whole-CONUS NetCDFs
          (unsigned boto3, atomic `.part`; a `(cycle, step)` not yet
          published is logged and skipped).

        Subsetting / retrospective for the gridded (raster) products is
        not yet supported (see :data:`_GRIDDED_SUBSET_MESSAGE`).

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: The written paths (NetCDFs for a whole-file
                download; Parquet tables for a subset / retrospective
                read), in order.

        Raises:
            NotImplementedError: For a subset / retrospective request
                against a gridded (raster) product.
        """
        if self._mode == "retrospective":
            return self._fetch_retrospective(products)
        if self._wants_subset():
            return self._fetch_operational_subset(products)
        out: list[Path] = []
        for product in tqdm(
            products, disable=not self._show_progress, desc="nwm", unit="file"
        ):
            fetched = self._fetch_one(product)
            if fetched is not None:
                out.append(fetched)
        return out

    def _reader(self) -> Any:
        """Return the pyramids `LabeledDataset` reader class (lazy import).

        Returns:
            The `pyramids.netcdf.LabeledDataset` class.

        Raises:
            ImportError: When the reader is unavailable (names the extra
                and the minimum pyramids version).
        """
        try:
            from pyramids.netcdf import LabeledDataset
        except ImportError as exc:
            raise ImportError(
                "NWM subsetting / retrospective reads need the pyramids "
                "LabeledDataset reader; install `pip install "
                "earthlens[nwm]` (pyramids-gis[parquet] >= 0.38.0)."
            ) from exc
        return LabeledDataset

    def _netcdf_reader(self) -> Any:
        """Return the pyramids `NetCDF` class (lazy import) for gridded reads.

        Returns:
            The `pyramids.netcdf.NetCDF` class.

        Raises:
            ImportError: When the reader is unavailable.
        """
        try:
            from pyramids.netcdf import NetCDF
        except ImportError as exc:
            raise ImportError(
                "NWM gridded subsetting needs the pyramids NetCDF reader; "
                "install `pip install earthlens[nwm]` "
                "(pyramids-gis[parquet] >= 0.38.0)."
            ) from exc
        return NetCDF

    def _feature_ids(self) -> list[int] | None:
        """Return the explicit `feature_id`s from `sites=`, or `None`.

        `sites=` may carry raw `feature_id` integers (selected directly)
        or USGS `gage_id` strings (joined via the in-file `gage_id`
        coord). Integer-valued entries are returned here; the `gage_id`
        strings are resolved by :meth:`_gage_ids`.

        Returns:
            list[int] | None: The integer `feature_id`s, or `None` when
                none were given.
        """
        if not self._sites:
            return None
        ids = [s for s in self._sites if _is_int(s)]
        return ids or None

    def _gage_ids(self) -> list[str] | None:
        """Return the USGS `gage_id` strings from `sites=`, or `None`."""
        if not self._sites:
            return None
        gages = [str(s) for s in self._sites if not _is_int(s)]
        return gages or None

    def _select_and_write(
        self, cube: Any, product: NWMProduct, stem: str, *, slice_time: bool
    ) -> Path:
        """Apply the request's label/bbox(/time) selection and write a table.

        Args:
            cube: An opened `LabeledDataset` for one tabular product.
            product: The resolved product row.
            stem: File-name stem for the written Parquet table.
            slice_time: Apply the `[start, end]` window on the `time`
                axis. `True` for the retrospective archive (the store
                spans the whole record); `False` for an operational file
                (already a single chosen `(cycle, step)` timestep).

        Returns:
            Path: The written `.parquet` path.
        """
        feature_ids = self._feature_ids()
        if feature_ids is not None:
            cube = cube.select(feature_id=feature_ids)
        gage_ids = self._gage_ids()
        if gage_ids is not None:
            cube = cube.select_by_coord("gage_id", gage_ids)
        if self._sites is None and self._wants_subset():
            cube = cube.select_bbox(self._bbox())
        if slice_time:
            cube = cube.select_time(self.time.start_date, self.time.end_date)
        out_path = self.root_dir / f"{stem}.parquet"
        return Path(cube.to_parquet(out_path))

    def _bbox(self) -> tuple[float, float, float, float]:
        """Return the request bbox as `(west, south, east, north)`."""
        return (
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        )

    def _subset_gridded_file(self, path: Path, product: NWMProduct) -> list[Path]:
        """Bbox-crop a downloaded operational gridded file → GeoTIFF(s).

        The operational file is whole-CONUS and single-timestep, so each
        requested variable is read with `NetCDF.subset(variable, time=0,
        bbox=…)` — a windowed read on the file's native grid (Lambert
        Conformal Conic) — and written as one GeoTIFF per variable. A
        `sites=` request is invalid here (a grid has no `feature_id`).

        Args:
            path: The downloaded whole-CONUS NetCDF.
            product: The resolved (raster) product row.

        Returns:
            list[Path]: One written GeoTIFF per requested variable.

        Raises:
            ValueError: When `sites=` was given for a gridded product.
        """
        if self._sites is not None:
            raise ValueError(
                f"sites= selects feature_ids and does not apply to the gridded "
                f"product {product.product!r}; use a bbox (lat_lim/lon_lim) to "
                "subset a gridded product."
            )
        netcdf = self._netcdf_reader()
        nc = netcdf.read_file(str(path))
        out: list[Path] = []
        names = self._requested[product.product] or list(product.variables)
        for variable in names:
            try:
                dataset = nc.subset(variable, time=0, bbox=self._bbox(), crs=4326)
            except ValueError as exc:
                if "has no 1-D coordinate variable" in str(exc):
                    close_quietly(nc)
                    raise NotImplementedError(
                        f"NWM variable {variable!r} has a vertical/layer dimension "
                        "interleaved between its y and x axes, which the pyramids "
                        "NetCDF.subset reader cannot window yet; request a "
                        "single-level variable (e.g. SNEQV, SNOWH, ACCET) or "
                        "download the whole file without a bbox."
                    ) from exc
                raise
            out_path = self.root_dir / f"{path.stem}_{variable}.tif"
            dataset.to_cog(str(out_path))
            out.append(Path(out_path))
        close_quietly(nc)
        return out

    def _fetch_retrospective(self, products: list[RemoteProduct]) -> list[Path]:
        """Read + slice the retrospective Zarr for each tabular product.

        Opens each tabular product's `retro_zarr` store anonymously and
        lazily through the pyramids `LabeledDataset` reader, applies the
        `sites=`/bbox/time selection, and writes a tidy `feature_id ×
        time` Parquet table. Gridded products are deferred (see
        :data:`_GRIDDED_RETRO_MESSAGE`).

        Args:
            products: The retrospective products from :meth:`_search`.

        Returns:
            list[Path]: One written Parquet path per tabular product.

        Raises:
            NotImplementedError: For a gridded (raster) product.
        """
        reader = self._reader()
        out: list[Path] = []
        for rp in tqdm(
            products, disable=not self._show_progress, desc="nwm-retro", unit="store"
        ):
            product = self._catalog.get_product(rp.metadata["product"])
            if product.output_kind != "tabular":
                raise NotImplementedError(_GRIDDED_RETRO_MESSAGE)
            cube = reader.read_file(
                rp.href, anon=True, variables=self._requested[product.product]
            )
            stem = (
                f"{product.product}_retro_"
                f"{self.time.start_date:%Y%m%d}_{self.time.end_date:%Y%m%d}"
            )
            out.append(self._select_and_write(cube, product, stem, slice_time=True))
        return out

    def _fetch_operational_subset(self, products: list[RemoteProduct]) -> list[Path]:
        """Download whole operational files, then read + subset each.

        The operational files are whole-CONUS, so a `sites=`/bbox subset
        first downloads each file (unsigned boto3) and then reads it.
        Routing is per product kind:

        * **tabular** (`chrtout`/`lakeout`/`coastal`) → `LabeledDataset`
          selects the `sites=`/bbox labels and writes a Parquet table;
        * **raster** (`ldasout`/`rtout`/`forcing`) → `NetCDF.subset`
          bbox-crops each variable on its native grid and writes
          GeoTIFF(s).

        The fetched NetCDF is left in place alongside the output (it is
        the as-fetched source).

        Args:
            products: The operational products from :meth:`_search`.

        Returns:
            list[Path]: The written output paths (Parquet for tabular,
                GeoTIFF for raster), in order.
        """
        reader = self._reader() if self.OUTPUT_KIND == "tabular" else None
        out: list[Path] = []
        for rp in tqdm(
            products, disable=not self._show_progress, desc="nwm", unit="file"
        ):
            downloaded = self._fetch_one(rp)
            if downloaded is None:
                continue
            product = self._catalog.get_product(rp.metadata["product"])
            if product.output_kind == "tabular":
                # reader is built whenever OUTPUT_KIND == "tabular" (above).
                assert reader is not None
                cube = reader.read_file(
                    str(downloaded), variables=self._requested[product.product]
                )
                out.append(
                    self._select_and_write(
                        cube, product, downloaded.stem, slice_time=False
                    )
                )
                close_quietly(cube)
            else:
                out.extend(self._subset_gridded_file(downloaded, product))
        return out

    def _client(self) -> Any:
        """Return the unsigned `boto3` S3 client for the public NWM bucket.

        Built once and cached on the instance: `_fetch_one` asks for it per
        product, and constructing a fresh `boto3` client each time would
        re-resolve endpoints and discard the connection pool.

        Returns:
            An anonymous `boto3` S3 client.

        Raises:
            ImportError: When `boto3` is not installed (names
                `earthlens[nwm]`).
        """
        if self._s3_client is not None:
            return self._s3_client
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.client import Config
        except ImportError as exc:
            raise ImportError(
                "the National Water Model backend needs `boto3`; install "
                "`pip install earthlens[nwm]`."
            ) from exc
        self._s3_client = boto3.client(
            "s3", region_name=self._region, config=Config(signature_version=UNSIGNED)
        )
        return self._s3_client

    def _fetch_one(self, product: RemoteProduct) -> Path | None:
        """Download one product's NetCDF file (atomic `.part` rename).

        The output name flattens the **full** S3 key (date prefix +
        configuration directory + basename), so it is unique per
        `(date, configuration, cycle, step, product, member)` — the NWM
        basename alone omits the date, so two days with the same
        cycle/step would otherwise collide and overwrite. The body is
        streamed to disk in chunks rather than buffered whole in memory
        (a `land` file can be ~220 MB).

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            Path | None: The written path, or `None` when the key was not
                published (logged and skipped).
        """
        client = self._client()
        key = product.href
        assert key is not None  # NWM products always carry an S3 key href
        target = self.root_dir / safe_filename(key)
        tmp = target.with_name(target.name + ".part")
        try:
            body = client.get_object(Bucket=BUCKET, Key=key)["Body"]
            with open(tmp, "wb") as handle:
                shutil.copyfileobj(body, handle)
            tmp.replace(target)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if _is_missing_key(exc):
                logger.warning(f"nwm: skipping {product.id} — not published ({key}).")
                return None
            raise
        return target

    def download(
        self,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch the requested NWM data and return the written paths.

        Runs the cheap :meth:`_search` (key enumeration) then
        :meth:`_fetch`, which routes by mode and subset:

        * a plain operational request downloads the whole-CONUS NetCDFs;
        * a `sites=`/bbox subset or `mode="retrospective"` reads + slices
          the tabular products (`chrtout`, `lakeout`, `coastal`) through
          the pyramids `LabeledDataset` reader and writes Parquet tables;
        * a subset / retrospective request for a gridded product raises
          `NotImplementedError`.

        Args:
            progress_bar: Show a per-item progress bar. Defaults to
                `True`.

        Returns:
            list[Path]: The written paths — whole-CONUS NetCDFs for a
                plain operational request, or Parquet tables for a
                subset / retrospective read. Empty when nothing in the
                window was available.
        """
        self._show_progress = progress_bar
        return self._api_via_search_fetch()


def _is_int(value: Any) -> TypeGuard[int]:
    """Return whether `value` is a genuine integer (and not a `bool`).

    `bool` is a subclass of `int`, so a plain `isinstance(value, int)`
    would read `sites=[True]` as `feature_id=1`; this excludes it.

    Args:
        value: A `sites=` entry (a `feature_id` int or a `gage_id` str).

    Returns:
        bool: `True` for an `int` that is not a `bool`.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_missing_key(exc: BaseException) -> bool:
    """Return whether `exc` is an S3 "key does not exist" error.

    Args:
        exc: The exception raised by a boto3 `get_object`.

    Returns:
        bool: `True` for a `NoSuchKey` / `404` client error.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    code = str(error.get("Code", ""))
    return code in {"NoSuchKey", "404", "NoSuchBucket"}
