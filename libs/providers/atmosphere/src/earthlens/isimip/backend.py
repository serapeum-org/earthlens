"""Backend that fetches ISIMIP bias-adjusted, impact-ready climate forcing.

`ISIMIP(AbstractDataSource)` resolves the ISIMIP repository of **bias-adjusted,
impact-model-ready climate forcing** (CMIP6-derived) — the pragmatic
non-stationary-futures input for flood / hydrology impact models. Unlike the
`cmip6` backend (raw CMIP6 on the Pangeo mirror), ISIMIP data is already
bias-corrected against W5E5 and formatted for impact models.

A request is a *facet set* — `dataset` (the `simulation_round`, e.g.
`"ISIMIP3b"`), `gcm` (the `climate_forcing`), `scenario` (`climate_scenario`),
`variables` (`climate_variable`s), `time_step` (+ `product`). :meth:`_search`
queries the ISIMIP REST API for the matching datasets and resolves each to its
per-decade NetCDF granule `path`s (filtered to the requested date window).
Because a single granule is ~1-2 GB (a whole global-daily dataset is ~18 GB),
:meth:`_fetch` never pulls them whole: it submits a **server-side cutout job**
(`isimip-client.cutout_bbox`: submit bbox -> poll -> download the cut zip),
extracts the cut NetCDF granules, and returns their `list[Path]`. earthlens never
imports `xarray` / `netCDF4` — reading the NetCDF is pyramids'.

The cutout is mandatory: a request must give a bbox (`lat_lim` / `lon_lim`) or
opt into a whole-globe pull with `whole_globe=True` (warned, since it downloads
the raw multi-GB granules). Aggregation (`aggregate=`) is refused — the written
NetCDFs can be reduced separately with `earthlens.aggregate.aggregate_netcdf`.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from loguru import logger
from tqdm import tqdm

from earthlens.base import AbstractDataSource, OutputKind, RemoteProduct, TemporalExtent
from earthlens.biodiversity import LicenseWarning
from earthlens.isimip._client import IsimipClient, build_client
from earthlens.isimip.catalog import Catalog

#: Trailing `_<start>_<end>.nc` decade range in an ISIMIP granule file name.
_FILE_RANGE_RE = re.compile(r"_(\d{4})_(\d{4})\.nc$")

#: Rights labels ISIMIP marks as freely reusable; anything else earns a warning.
_OPEN_RIGHTS = frozenset({"cc0 1.0", "cc by 4.0"})


class ISIMIP(AbstractDataSource):
    """ISIMIP bias-adjusted climate-forcing backend (repository REST API + cutout).

    Wraps the ISIMIP repository so a user pulls a bias-adjusted forcing subset —
    round / GCM / scenario / variable — through the same `download()` shape every
    other earthlens backend uses. The output is one NetCDF granule per resolved
    (dataset, decade) file, cut to the requested bbox by the ISIMIP cutout job.

    Attributes:
        OUTPUT_KIND: `"raster"` — the written artefacts are gridded NetCDFs.
    """

    OUTPUT_KIND: OutputKind = "raster"

    AGGREGATE_REFUSAL_REASON = (
        "the backend writes bias-adjusted NetCDF granules; reduce them separately "
        "with earthlens.aggregate.aggregate_netcdf"
    )

    def __init__(  # NOSONAR - S107: one keyword per ISIMIP request facet; the facade forwards these flat, matching every sibling backend (e.g. cmip6)
        self,
        start: str,
        end: str,
        *,
        dataset: str = "ISIMIP3b",
        variables: list[str] | None = None,
        scenario: str | None = None,
        gcm: str | None = None,
        product: str = "InputData",
        temporal_resolution: str = "daily",
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        whole_globe: bool = False,
        poll: float = 4.0,
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        catalog: Catalog | None = None,
        client: IsimipClient | None = None,
    ):
        """Initialise an ISIMIP backend instance.

        Args:
            start: Inclusive start of the date window (parsed with `fmt`).
            end: Inclusive end of the date window.
            dataset: The `simulation_round` (`"ISIMIP3b"` / `"ISIMIP3a"`).
            variables: The `climate_variable`s to fetch (`["pr"]`, `["tas"]`).
            scenario: The `climate_scenario` (`"ssp585"`, `"historical"`).
            gcm: The `climate_forcing` (`"gfdl-esm4"`); any casing is accepted
                and lowercased to the API spelling.
            product: `"InputData"` (bias-adjusted forcing) or `"OutputData"`
                (impact-model results). Defaults to `"InputData"`.
            temporal_resolution: The ISIMIP `time_step` — `"daily"` or
                `"monthly"`. Defaults to `"daily"`.
            lat_lim: `[lat_min, lat_max]` in degrees for the cutout bbox.
            lon_lim: `[lon_min, lon_max]` in degrees for the cutout bbox.
            whole_globe: Skip the cutout and download the raw whole-globe
                granules (warned — each is ~1-2 GB). Defaults to `False`; a
                request must give a bbox or set this. When `True` it takes
                precedence over any `lat_lim` / `lon_lim` (a warning is logged).
            poll: Seconds between cutout-job status polls. Defaults to `4.0`.
            path: Output directory for the written NetCDFs.
            fmt: `strptime` format for `start` / `end`.
            catalog: Optional pre-built :class:`Catalog`; defaults to the bundled
                catalog.
            client: Optional pre-built :class:`IsimipClient`; defaults to a
                lazily-built `isimip-client` client (needs the `[isimip]` extra).

        Raises:
            ValueError: If a required facet (`scenario` / `gcm` / `variables`) is
                omitted, a facet is not in the catalog vocabulary, a date bound is
                empty, or no bbox is given without `whole_globe`.
        """
        self._catalog = catalog if catalog is not None else Catalog()
        self._client = client

        if not variables:
            raise ValueError("ISIMIP requires a non-empty variables list, e.g. ['pr'].")
        if not scenario:
            raise ValueError("ISIMIP requires a scenario, e.g. scenario='ssp585'.")
        if not gcm:
            raise ValueError("ISIMIP requires a gcm, e.g. gcm='gfdl-esm4'.")
        if not start or not end:
            raise ValueError(
                "ISIMIP requires a start and end date, e.g. "
                "start='2030-01-01', end='2040-12-31'."
            )
        if poll <= 0:
            raise ValueError(
                f"ISIMIP poll must be a positive number of seconds, got {poll!r}."
            )

        self._round = dataset
        self._product = product
        self._time_step = temporal_resolution
        self._scenario = scenario
        self._gcm = Catalog.normalize_forcing(gcm)
        self._variables = list(variables)
        self._whole_globe = whole_globe
        self._poll = poll
        self._show_progress = True

        self._validate_facets()

        super().__init__(
            start=start,
            end=end,
            variables=self._variables,
            temporal_resolution=temporal_resolution,
            lat_lim=[-90.0, 90.0] if lat_lim is None else lat_lim,
            lon_lim=[-180.0, 180.0] if lon_lim is None else lon_lim,
            fmt=fmt,
            path=path,
        )

        if self._bbox() is None and not self._whole_globe:
            raise ValueError(
                "ISIMIP requires a bbox (lat_lim / lon_lim) so the granule is cut "
                "server-side; pass whole_globe=True to download the raw ~1-2 GB "
                "global granules instead."
            )
        if self._whole_globe and self._bbox() is not None:
            logger.warning(
                "isimip: whole_globe=True takes precedence over the given "
                "lat_lim/lon_lim; downloading the raw global granules, not a cutout."
            )

    def _validate_facets(self) -> None:
        """Validate every requested facet against the catalog vocabulary.

        Raises:
            ValueError: If the round / product / time-step / scenario / GCM or any
                variable is not in the catalog (each with a did-you-mean hint).
        """
        self._catalog.get_round(self._round)
        self._catalog.get_scenario(self._scenario)
        self._catalog.get_forcing(self._gcm)
        for var in self._variables:
            self._catalog.get_dataset(var)
        if self._product not in self._catalog.products:
            raise ValueError(
                f"{self._product!r} is not an ISIMIP product. "
                f"Known products: {self._catalog.products}."
            )
        if self._time_step not in self._catalog.time_steps:
            raise ValueError(
                f"{self._time_step!r} is not an ISIMIP time_step. "
                f"Known time_steps: {self._catalog.time_steps}."
            )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        Args:
            start: Inclusive window start.
            end: Inclusive window end.
            temporal_resolution: Advisory cadence label (`"daily"` / `"monthly"`).
            fmt: `strptime` format tried first for a string bound.

        Returns:
            TemporalExtent: Frozen model with the parsed bounds.
        """
        return self._whole_window_extent(start, end, fmt=fmt, resolution="raw")

    def _wants_spatial_subset(self) -> bool:
        """Return whether the request narrows the grid (a bbox crop).

        Returns:
            bool: `True` when the bbox is narrower than whole-Earth.
        """
        return not (
            self.space.latitude_min <= -90.0
            and self.space.latitude_max >= 90.0
            and self.space.longitude_min <= -180.0
            and self.space.longitude_max >= 180.0
        )

    def _bbox(self) -> tuple[float, float, float, float] | None:
        """Return the request bbox as `(west, east, south, north)`, or `None`.

        Returns:
            tuple | None: The cutout window in the `isimip-client` argument order,
                or `None` for a whole-globe request.
        """
        if not self._wants_spatial_subset():
            return None
        return (self.space.west, self.space.east, self.space.south, self.space.north)

    def _client_or_build(self) -> IsimipClient:
        """Return the injected client, else lazily build the real SDK client.

        Returns:
            IsimipClient: The client used for search + cutout + download.

        Raises:
            ModuleNotFoundError: If no client was injected and `isimip-client`
                is not installed.
        """
        if self._client is None:
            self._client = build_client(
                self._catalog.data_url, self._catalog.files_api_url
            )
        return self._client

    def _file_in_window(self, name: str) -> bool:
        """Return whether a granule file overlaps the requested date window.

        Args:
            name: The granule file name (e.g.
                `gfdl-esm4_..._pr_global_daily_2015_2020.nc`).

        Returns:
            bool: `True` if the file's decade range overlaps `[start, end]`, or if
                the name carries no `_<year>_<year>` range (kept to be safe).
        """
        match = _FILE_RANGE_RE.search(name)
        if match is None:
            return True
        file_start, file_end = int(match.group(1)), int(match.group(2))
        return not (
            file_end < self.time.start_date.year or file_start > self.time.end_date.year
        )

    def _search(self) -> list[RemoteProduct]:
        """Resolve the facet set to one product per matching ISIMIP dataset.

        For each requested variable, query the ISIMIP REST API for the matching
        datasets and keep the per-decade granule `path`s that overlap the date
        window.

        Returns:
            list[RemoteProduct]: One product per resolved dataset; each carries
                the in-window file `paths` / `urls`, the dataset `name`, and its
                `rights` / `restricted` licence flags in `metadata`.

        Raises:
            ValueError: If any requested variable matches no dataset, or no
                granule overlaps the window (a missing granule is never silently
                skipped).
        """
        client = self._client_or_build()
        products: list[RemoteProduct] = []
        for var in self._variables:
            datasets = client.datasets(
                simulation_round=self._round,
                product=self._product,
                climate_forcing=self._gcm,
                climate_scenario=self._scenario,
                climate_variable=var,
                time_step=self._time_step,
            )
            if not datasets:
                raise ValueError(
                    f"ISIMIP: no dataset for climate_variable={var!r} "
                    f"(round={self._round}, gcm={self._gcm}, scenario={self._scenario}, "
                    f"product={self._product}, time_step={self._time_step})."
                )
            var_products = 0
            for dataset in datasets:
                files = [
                    f
                    for f in dataset.get("files", [])
                    if self._file_in_window(f.get("name", ""))
                ]
                if not files:
                    logger.warning(
                        f"isimip: dataset {dataset.get('name')!r} has no granule "
                        f"overlapping {self.time.start_date.year}-"
                        f"{self.time.end_date.year}; skipping it."
                    )
                    continue
                rights = dataset.get("rights") or {}
                products.append(
                    RemoteProduct(
                        id=str(dataset.get("name") or dataset.get("id")),
                        metadata={
                            "paths": [f["path"] for f in files],
                            "urls": [f.get("file_url") for f in files],
                            "name": dataset.get("name"),
                            "rights": rights.get("short", ""),
                            "restricted": bool(dataset.get("restricted")),
                        },
                    )
                )
                var_products += 1
            if var_products == 0:
                raise ValueError(
                    f"ISIMIP: climate_variable={var!r} matched datasets but no "
                    "granule overlaps the requested window "
                    f"[{self.time.start_date.date()}, {self.time.end_date.date()}]; "
                    "widen the date window or drop the variable."
                )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Cut each resolved dataset to the bbox and write the NetCDF granules.

        For a bbox request: submit a cutout job per product (`cutout_bbox`), poll
        to `finished`, then download + extract the cut NetCDFs. For a
        `whole_globe` request: download the raw granules directly.

        Args:
            products: The products from :meth:`_search`.

        Returns:
            list[Path]: The written NetCDF paths, in product order.

        Raises:
            RuntimeError: If a cutout job does not reach `finished`.
        """
        client = self._client_or_build()
        out: list[Path] = []
        for product in tqdm(
            products, disable=not self._show_progress, desc="isimip", unit="dataset"
        ):
            self._warn_license(product)
            if self._whole_globe:
                out.extend(self._download_whole(client, product))
                continue
            bbox = self._bbox()
            assert bbox is not None  # the constructor requires a bbox here
            west, east, south, north = bbox
            job = client.cutout_bbox(
                product.metadata["paths"], west, east, south, north, poll=self._poll
            )
            if not job or job.get("status") != "finished":
                raise RuntimeError(
                    f"ISIMIP cutout job for {product.id!r} did not finish: "
                    f"status={None if not job else job.get('status')!r}."
                )
            file_url = job.get("file_url")
            if not file_url:
                raise RuntimeError(
                    f"ISIMIP cutout job for {product.id!r} finished without a "
                    "file_url to download."
                )
            out.extend(self._download_extract(client, product, file_url))
        return out

    def _product_dir(self, product: RemoteProduct) -> Path:
        """Return a per-product, per-window, per-region output subdirectory.

        Each resolved dataset writes into its own subdirectory keyed by the
        dataset id, the requested `[start, end]` window, and the bbox region
        (`global` for a whole-globe request). :meth:`_download_extract` returns
        every `*.nc` it finds in that directory, so keying on all three means
        concurrent products never collide, re-running the same request just
        overwrites in place, and re-running the same dataset/window with a
        *different* bbox never picks up the earlier run's differently-cut
        granule (each bbox gets its own directory).

        Args:
            product: The product whose output subdirectory to build.

        Returns:
            Path: The created per-product directory.
        """
        window = f"{self.time.start_date.year}_{self.time.end_date.year}"
        bbox = self._bbox()
        # A whole-globe pull always writes to a `global` region dir, even when a
        # bbox was also supplied (whole_globe wins) — so raw global granules
        # never share a directory with a same-bbox cutout run.
        region = (
            "global"
            if (self._whole_globe or bbox is None)
            else "w{}e{}s{}n{}".format(*bbox)
        )
        raw = f"{product.id}_{region}_{window}"
        slug = re.sub(r"[^0-9A-Za-z._-]+", "_", raw) or "isimip"
        directory = self._ensure_root_dir() / slug
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _download_extract(
        self, client: IsimipClient, product: RemoteProduct, file_url: str
    ) -> list[Path]:
        """Download a finished cutout zip and return the extracted NetCDFs.

        Args:
            client: The active ISIMIP client.
            product: The product the cutout belongs to (its output subdirectory).
            file_url: The finished job's `file_url` (a `.zip`).

        Returns:
            list[Path]: The `*.nc` files the zip yielded, under the product dir.

        Raises:
            RuntimeError: If the cutout produced no NetCDF granule.
        """
        directory = self._product_dir(product)
        client.download(file_url, path=str(directory), extract=True)
        found = sorted(directory.rglob("*.nc"))
        if not found:
            raise RuntimeError(
                f"ISIMIP cutout for {product.id!r} produced no NetCDF granule."
            )
        # Drop the downloaded cutout archive + its README so the output dir holds
        # only the extracted NetCDFs.
        for clutter in (*directory.glob("*.zip"), *directory.glob("README.txt")):
            clutter.unlink(missing_ok=True)
        return found

    def _download_whole(
        self, client: IsimipClient, product: RemoteProduct
    ) -> list[Path]:
        """Download the raw whole-globe granules for a `whole_globe` request.

        Args:
            client: The active ISIMIP client.
            product: The product whose raw granule `urls` to download.

        Returns:
            list[Path]: The downloaded `*.nc` paths, under the product dir.

        Raises:
            RuntimeError: If no granule carried a downloadable URL.
        """
        logger.warning(
            f"isimip: whole_globe download for {product.id!r} — pulling the raw "
            "~1-2 GB global granule(s); pass lat_lim/lon_lim to cut server-side."
        )
        directory = self._product_dir(product)
        for url in product.metadata["urls"]:
            if not url:
                continue
            client.download(url, path=str(directory))
        found = sorted(directory.rglob("*.nc"))
        if not found:
            raise RuntimeError(
                f"ISIMIP whole-globe download for {product.id!r} produced no "
                "granule with a downloadable URL."
            )
        return found

    def _warn_license(self, product: RemoteProduct) -> None:
        """Emit a :class:`LicenseWarning` for a restricted, non-open, or unknown-licence dataset.

        A dataset whose `rights` are empty is treated as unknown (warned), so a
        licence the API does not report is surfaced rather than assumed open.

        Args:
            product: The product whose licence flags to check.
        """
        rights = str(product.metadata.get("rights", ""))
        restricted = bool(product.metadata.get("restricted"))
        if restricted or rights.lower() not in _OPEN_RIGHTS:
            warnings.warn(
                f"ISIMIP dataset {product.id!r} carries non-open terms "
                f"(rights={rights or 'unknown'!r}, restricted={restricted}); "
                "review the ISIMIP terms of use before redistributing.",
                LicenseWarning,
                stacklevel=2,
            )

    def terms_note(self) -> str:
        """Return the documentation licence note for the requested round.

        Returns:
            str: The round's `default_license` (the authoritative per-dataset
                licence is surfaced live via :meth:`_warn_license`).
        """
        return self._catalog.get_round(self._round).default_license

    def download(self, progress_bar: bool = True) -> list[Path]:
        """Fetch the requested bias-adjusted granules and return the written paths.

        Runs the cheap :meth:`_search` (facet -> dataset resolution) then
        :meth:`_fetch`, which submits the cutout job(s) and writes one cut NetCDF
        per resolved granule.

        Args:
            progress_bar: Show a per-dataset progress bar. Defaults to `True`.

        Returns:
            list[Path]: The written NetCDF paths (never empty — a facet set that
                matches nothing raises rather than returning an empty list).

        Raises:
            ValueError: If the facet set matches no dataset / granule.
            RuntimeError: If a cutout job does not finish.
        """
        self._show_progress = progress_bar
        return self._api_via_search_fetch()
