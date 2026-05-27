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
* **Whole-CONUS download MVP.** An operational NWM file is whole-CONUS
  (~14 MB for `channel_rt`, ~30 MB for `land`). The MVP downloads the
  whole files (boto3, no read). **Any subset** — by `sites=` /
  `feature_id` / a narrower bbox, or `mode="retrospective"` (the 1.4 TB
  Zarr) — needs a *read*, which is a pyramids capability (`PY-G`,
  unreleased), so it raises a clear `NotImplementedError` naming `PY-G`.
  earthlens never imports `xarray` / `zarr` directly.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.nwm.catalog import Catalog, NWMConfig, NWMProduct

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

#: The unsigned AWS bucket holding NWM operational output.
BUCKET = "noaa-nwm-pds"

#: The retrospective (v3.0) Zarr bucket — reached only through the
#: `PY-G` pyramids reader (unreleased), never downloaded whole.
RETRO_BUCKET = "noaa-nwm-retrospective-3-0-pds"

#: Approximate operational retention: `noaa-nwm-pds` keeps a rolling
#: archive (~500+ days as of 2026-05). A window ending before this many
#: days ago auto-routes to the retrospective mode.
OPERATIONAL_RETENTION_DAYS = 500

#: Shared `PY-G` deferral message — every subset path raises this.
_PYG_MESSAGE = (
    "needs the pyramids cloud-Zarr / feature_id-NetCDF reader (PY-G), which is "
    "unreleased. NWM operational files are whole-CONUS, so any subset requires a "
    "read; earthlens does not import xarray/zarr directly. Request the whole "
    "files (no sites=/feature_id and a whole-Earth bbox) to download them, or "
    "wait for the pyramids PY-G reader for subsetting and the retrospective Zarr."
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
    day = start.date()
    while day <= end.date():
        for hour in sorted(cycles_utc):
            out.append(dt.datetime(day.year, day.month, day.day, hour))
        day += dt.timedelta(days=1)
    return out


def build_key(
    config: NWMConfig, product: NWMProduct, cycle: dt.datetime, step: int, member: int
) -> str:
    """Assemble the S3 object key for one `(config, product, cycle, step)`.

    Mirrors the verified `noaa-nwm-pds` layout: deterministic runs use the
    `{family}` directory and the product's `s3_token`; ensemble runs use a
    `{family}_mem{member}` directory and ride the member on the product
    token (`channel_rt_1`). Forecast steps format as `fNNN`, analysis
    steps as `tmNN`. The domain suffixes the file name.

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
        directory = f"{config.family}_mem{member}"
        token = f"{product.s3_token}_{member}"
    else:
        directory = config.family
        token = product.s3_token
    step_token = f"f{step:03d}" if config.step_kind == "forecast" else f"tm{step:02d}"
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

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str = "",
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
                (`[-90, 90]`) means "no spatial subset"; a narrower box
                is a subset request and is `PY-G`-gated (see the module
                docstring).
            lon_lim: `[lon_min, lon_max]` in degrees.
            temporal_resolution: Advisory label (NWM cadence is fixed by
                the configuration).
            path: Output directory for the fetched NetCDF files.
            fmt: `strptime` format for `start` / `end`.
            configuration: The operational configuration key
                (`"short_range"`, `"analysis_assim"`, `"medium_range"`).
            mode: `"operational"` (NetCDF on `noaa-nwm-pds`) or
                `"retrospective"` (Zarr, `PY-G`-gated). `None` auto-routes
                by the date window.
            member: Ensemble member (1-based) for an ensemble
                configuration; ignored for deterministic ones.
            cycles: Restrict the run hours fetched (a subset of the
                configuration's `cycles_utc`); defaults to every cycle.
            steps: Explicit steps to fetch; wins over `horizon`.
            horizon: Maximum step; expands from the configuration's
                `first_step` on its `step_cadence_h`.
            sites: Explicit `feature_id`s / USGS `gage_id`s to subset to
                — `PY-G`-gated (any non-`None` value raises).
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
        self._show_progress = True

        self._products: list[NWMProduct] = []
        for product_key, names in variables.items():
            product = self._catalog.get_product(product_key)
            if configuration not in product.configurations:
                raise ValueError(
                    f"product {product_key!r} is not published under "
                    f"configuration {configuration!r}; it is available in "
                    f"{product.configurations}."
                )
            unknown = [n for n in names if n not in product.variables]
            if unknown:
                raise ValueError(
                    f"variable(s) {unknown} are not in product {product_key!r}; "
                    f"available: {sorted(product.variables)}."
                )
            self._products.append(product)

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

    def _initialize(self) -> None:
        """No-op auth hook — the NWM bucket is anonymous. Returns `None`."""
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the user bbox into a :class:`SpatialExtent` (no snapping)."""
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the cycle-date window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label.
            fmt: `strptime` format applied to `start` / `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="raw",
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

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
        """Return whether the request asks for a subset (needs `PY-G`).

        A subset is requested when `sites=` was given or the bbox is
        narrower than whole-Earth. Operational files are whole-CONUS, so
        any subset needs a read.

        Returns:
            bool: `True` when a subset (and therefore `PY-G`) is needed.
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
        requested NWM product carrying the Zarr store URI (handled by the
        `PY-G`-gated fetch).

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
        """Download each product's whole-CONUS NetCDF into the output dir.

        Any subset path (`sites=` / a narrower bbox, or
        `mode="retrospective"`) raises `NotImplementedError` naming
        `PY-G`. Otherwise each S3 key is fetched (unsigned boto3, atomic
        `.part` rename); a `(cycle, step)` not yet published is logged and
        skipped so one miss does not lose the rest.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: One fetched NetCDF path per successful product,
                in order. Shorter than `products` when some were skipped.

        Raises:
            NotImplementedError: For any subset / retrospective request
                (names `PY-G`).
        """
        if self._mode == "retrospective":
            raise NotImplementedError(f"NWM mode='retrospective' {_PYG_MESSAGE}")
        if self._wants_subset():
            raise NotImplementedError(f"NWM subsetting (sites=/bbox) {_PYG_MESSAGE}")
        client = self._client()
        out: list[Path] = []
        for product in tqdm(
            products, disable=not self._show_progress, desc="nwm", unit="file"
        ):
            fetched = self._fetch_one(client, product)
            if fetched is not None:
                out.append(fetched)
        return out

    def _client(self) -> Any:
        """Build an unsigned `boto3` S3 client for the public NWM bucket.

        Returns:
            An anonymous `boto3` S3 client.

        Raises:
            ImportError: When `boto3` is not installed (names
                `earthlens[nwm]`).
        """
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.client import Config
        except ImportError as exc:
            raise ImportError(
                "the National Water Model backend needs `boto3`; install "
                "`pip install earthlens[nwm]`."
            ) from exc
        return boto3.client(
            "s3", region_name=self._region, config=Config(signature_version=UNSIGNED)
        )

    def _fetch_one(self, client: Any, product: RemoteProduct) -> Path | None:
        """Download one product's NetCDF file (atomic `.part` rename).

        Args:
            client: The unsigned boto3 client.
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            Path | None: The written path, or `None` when the key was not
                published (logged and skipped).
        """
        key = product.href
        target = self.root_dir / Path(key).name
        tmp = target.with_name(target.name + ".part")
        try:
            body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
            with open(tmp, "wb") as handle:
                handle.write(body)
            tmp.replace(target)
        except BaseException as exc:
            tmp.unlink(missing_ok=True)
            if _is_missing_key(exc):
                logger.warning(f"nwm: skipping {product.id} — not published ({key}).")
                return None
            raise
        return target

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Fetch the requested NWM files and return the written paths.

        Runs the cheap :meth:`_search` (key enumeration) then
        :meth:`_fetch` (the whole-CONUS downloads). A subset request
        (`sites=` / a narrower bbox / `mode="retrospective"`) raises
        `NotImplementedError` naming `PY-G`.

        Args:
            progress_bar: Show a per-file progress bar. Defaults to
                `True`.
            aggregate: Must be `None`. NWM `chrtout` is feature-id
                indexed (not griddable) and a gridded `ldasout` reduce
                needs a read (`PY-G`), so aggregation is unsupported. The
                facade already rejects a non-`None` `aggregate=` for a
                non-raster backend; this guards direct callers.

        Returns:
            list[Path]: The whole-CONUS NetCDF paths written, in order.
                Empty when nothing in the window was available.

        Raises:
            NotImplementedError: If `aggregate` is not `None`, or for any
                subset / retrospective request (names `PY-G`).
        """
        if aggregate is not None:
            raise NotImplementedError(
                "NWM.download(aggregate=...) is not supported — chrtout is "
                "feature-id indexed (not griddable) and a gridded ldasout reduce "
                "needs the pyramids reader (PY-G)."
            )
        self._show_progress = progress_bar
        return self._api_via_search_fetch()


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
