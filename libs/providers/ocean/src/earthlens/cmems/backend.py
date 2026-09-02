"""Backend that subsets Copernicus Marine datasets via the toolbox.

`CMEMS(AbstractDataSource)` accepts the same constructor surface as
the other earthlens backends — `start`, `end`, `variables`,
`lat_lim`, `lon_lim`, `temporal_resolution`, `path` — plus a few
backend-specific kwargs for authentication and output format. Each
`(dataset_id, [variables, ...])` pair in the `variables` mapping
becomes one server-side `copernicusmarine.subset()` call. The
toolbox cuts the requested space/time window out of the source
dataset on the server and streams a single NetCDF (or Zarr) back to
the user's `path`.

The on-disk artefact is a gridded NetCDF/Zarr, so
`OUTPUT_KIND = "raster"` — structurally identical to ECMWF's
per-variable NetCDF output. The :class:`earthlens.earthlens.EarthLens`
facade therefore forwards `aggregate=AggregationConfig(...)` to this
backend's `download()`. The aggregation runs through
:meth:`pyramids.netcdf.NetCDF.reduce` (the generalised time-window
reducer shipped in pyramids): the optional `depth` axis is collapsed
to a column mean (or pinned with `AggregationConfig.level`), the
`time` axis is windowed by `AggregationConfig.freq`, and one GeoTIFF
per `(variable, window)` is written — the same output shape the ECMWF
backend produces via :func:`earthlens.aggregate.aggregate_netcdf`.
The window labels are computed from the file's own decoded time axis
because pyramids' native `get_time_variable` does not decode the CMEMS
time coordinate; see :meth:`CMEMS._window_labels`. A pyramids build
without `NetCDF.reduce` raises a clear `NotImplementedError`.
"""

from __future__ import annotations

from collections import Counter
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from loguru import logger
from pydantic import SecretStr

from earthlens.base import (
    CADENCE_ALIASES,
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    safe_filename,
    window_labels,
)
from earthlens.cmems.auth import (
    AuthenticationError,
    CmemsAuth,
    CmemsCredentials,
)

if TYPE_CHECKING:
    from pyramids.netcdf import NetCDF

    from earthlens.aggregate import AggregationConfig


FileFormat = Literal["netcdf", "zarr"]


def _describe_product(product: RemoteProduct) -> str:
    """Render a product for the `_run_items` log lines.

    Args:
        product: The product whose subset failed.

    Returns:
        str: The CMEMS dataset id.
    """
    return str(product.id)


class CMEMS(AbstractDataSource):
    """Copernicus Marine Service backend (gridded NetCDF/Zarr output).

    Wraps :func:`copernicusmarine.subset` so a user can request a
    space/time window of one or more CMEMS datasets through the
    same `download()` shape every other earthlens backend uses.
    Each `(dataset_id, [variables])` pair becomes one server-side
    subset call; the toolbox returns a single NetCDF (or Zarr) per
    request, ready to be opened by any NetCDF reader — within this
    package, that reader is :class:`pyramids.netcdf.NetCDF`.

    Authentication is one-time: the first :meth:`_initialize` call
    delegates to :class:`CmemsAuth`, which validates the credentials
    against the Copernicus Marine portal. After that, every
    `subset()` call in this process — and in future processes that
    read the same `~/.copernicusmarine/` config directory — is
    authenticated automatically.

    Attributes:
        OUTPUT_KIND: `"raster"` — the on-disk artefact is a gridded
            NetCDF/Zarr, structurally identical to ECMWF's per-
            variable NetCDF output. Composes with the
            :class:`earthlens.aggregate.AggregationConfig` flow:
            `download(aggregate=...)` reduces each subset via
            :meth:`pyramids.netcdf.NetCDF.reduce` (depth collapse +
            time windowing) into per-`(variable, window)` GeoTIFFs.
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    AGGREGATE_REFUSAL_REASON = "this dataset resolves to a non-gridded output; only the gridded CMEMS datasets can be reduced"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        service_username: str | None = None,
        service_password: str | None = None,
        credentials_file: Path | str | None = None,
        file_format: FileFormat = "netcdf",
        minimum_depth: float | None = None,
        maximum_depth: float | None = None,
        overwrite: bool = True,
    ):
        """Initialise a CMEMS backend instance.

        Args:
            start: Inclusive start date as a string (parsed with
                `fmt`).
            end: Inclusive end date as a string.
            variables: Mapping from CMEMS dataset id to a list of
                variable short names drawn from that dataset, e.g.
                `{"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao",
                "so"]}`. The dataset id must be one that
                `copernicusmarine.describe()` recognises (curation
                in the catalog YAML is a metadata convenience, not
                a gate — uncurated dataset ids still work).
            lat_lim: `[lat_min, lat_max]` in degrees, both in
                `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` in degrees, both in
                `[-180, 180]`.
            temporal_resolution: The requested cadence, validated against
                `earthlens.base.CADENCE_ALIASES` (which covers every cadence the
                catalog rows declare). An unrecognised value raises `ValueError`
                listing the accepted spellings.
            path: Output directory. Created by the parent class if
                it does not exist. When omitted it falls back to the
                configured earthlens output directory (`set_output_dir()` /
                `EARTHLENS_DATA_DIR`); see `earthlens.config`.
            fmt: `strptime` format for `start` / `end`. Defaults
                to `"%Y-%m-%d"`.
            service_username: Copernicus Marine portal username.
                Falls back to
                `COPERNICUSMARINE_SERVICE_USERNAME` env var,
                then to the saved credentials file produced by a
                previous `copernicusmarine login`.
            service_password: Account password. Falls back to
                `COPERNICUSMARINE_SERVICE_PASSWORD` env var, then
                to the saved credentials file.
            credentials_file: Path to a pre-existing
                `.copernicusmarine-credentials` file. Useful for CI
                runners that mount the credentials as a secret.
            file_format: Output format — `"netcdf"` (default,
                single `.nc` per request) or `"zarr"` (one
                directory-store per request, suitable for very
                large requests).
            minimum_depth: Optional lower bound on the vertical
                axis, in metres. Defaults to `None` (no clipping).
            maximum_depth: Optional upper bound on the vertical
                axis, in metres. Defaults to `None`.
            overwrite: Whether `copernicusmarine.subset()` should
                overwrite an existing output file. Defaults to
                `True` to match the "fresh download each call"
                expectation of the other backends.
        """
        self._service_username = service_username
        self._service_password = service_password
        self._credentials_file = (
            Path(credentials_file) if credentials_file is not None else None
        )
        self._file_format: FileFormat = file_format
        self._minimum_depth = minimum_depth
        self._maximum_depth = maximum_depth
        self._overwrite = overwrite
        self._auth: CmemsAuth | None = None
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

    def _initialize(self):
        """Build the :class:`CmemsAuth`; defer `configure()` to download.

        Returns `None` so the parent class does not bind any opaque
        object to `self.client` — the toolbox has no per-instance client;
        credentials live in a config file that every subsequent
        `copernicusmarine.subset()` call reads. The actual
        :meth:`CmemsAuth.configure` (which contacts the auth server) is
        deferred to the first :meth:`download`, so constructing the
        backend never authenticates and `_search()` stays offline.
        """
        creds = CmemsCredentials(
            username=self._service_username,
            password=(
                SecretStr(self._service_password)
                if self._service_password is not None
                else None
            ),
            credentials_file=self._credentials_file,
        )
        self._auth = CmemsAuth(creds)
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the date range.

        Stores `start_date` / `end_date` as `datetime.datetime` (the
        toolbox accepts both `datetime` and ISO strings), the
        pandas `freq` alias matching `temporal_resolution`, and the
        full `DatetimeIndex` so downstream code that iterates per
        day / month sees the same shape every other backend
        exposes.

        Args:
            start: Inclusive start date as a string.
            end: Inclusive end date as a string.
            temporal_resolution: The requested cadence. Resolved through
                `earthlens.base.CADENCE_ALIASES`, which covers every cadence
                the CMEMS catalog rows declare; a periodic one expands
                `dates` to its period starts, while a release-character one
                (`"irregular"`, `"climatology"`) collapses `dates` to the two
                endpoints. An unrecognised cadence raises rather than
                silently substituting daily.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen pydantic model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than
                `end`.
        """
        return self._cadence_extent(
            start,
            end,
            fmt=fmt,
            cadence=temporal_resolution,
            accepted=CADENCE_ALIASES,
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        errors: str = "warn",
    ) -> list[Path]:
        """Subset every `(dataset_id, variables)` pair in `self.vars`.

        Each pair becomes one server-side
        :func:`copernicusmarine.subset` call; the toolbox returns
        the absolute path to the NetCDF (or Zarr store) it wrote.
        Failures on one pair are logged and surface in a summary
        line at the end of the loop, but do not abort the remaining
        pairs — mirrors the ECMWF backend's "one bad variable does
        not kill the batch" policy.

        Args:
            progress_bar: When `False`, the toolbox's per-request
                progress bar is suppressed. Defaults to `True`.
            aggregate: Optional
                :class:`earthlens.aggregate.AggregationConfig`.
                Accepted because :data:`OUTPUT_KIND` is `"raster"`,
                so the facade allows the kwarg through. When set,
                every subset NetCDF is reduced via
                :meth:`pyramids.netcdf.NetCDF.reduce`: any `depth`
                axis is collapsed first (mean) or pinned
                (`config.level`), then the `time` axis is windowed by
                `config.freq` with the `config.op` operator, and one
                GeoTIFF per `(variable, window)` is written (shaped
                like the ECMWF aggregate output). Requires a pyramids
                build that ships `NetCDF.reduce`
                (see :meth:`_aggregate_outputs`).

        Returns:
            list[Path]: When `aggregate` is `None`, the absolute
                paths of every subset the toolbox wrote (iteration
                order of `self.vars`; partial failure returns the
                successes; an empty list only for an empty request).
                When `aggregate` is set, the per-`(variable, window)`
                GeoTIFF paths instead.

        Raises:
            AuthenticationError: When :meth:`CmemsAuth.configure` fails
                (no credentials, rejected credentials, or the auth server
                unreachable) — now surfaced on first download rather than
                at construction.
            NotImplementedError: When `aggregate` is set but the
                installed pyramids has no `NetCDF.reduce`.
            ValueError: If `errors` is not a recognised policy.
            RuntimeError: When **every** `(dataset_id, variables)`
                pair fails its subset (total failure). Raised rather
                than returning `[]` so a caller cannot silently
                process nothing; the message aggregates the failed
                dataset ids and exception types, and the per-product
                toolbox exceptions are logged at ERROR.
        """
        self._errors = self.check_errors_policy(errors)
        # Authenticate lazily on first download (deferred out of __init__).
        assert self._auth is not None  # set by _initialize() before download()
        self._auth.configure()
        out_paths = self._api_via_search_fetch_with_progress(progress_bar)

        if aggregate is not None:
            return self._aggregate_outputs(out_paths, aggregate)

        if out_paths:
            logger.info(
                f"CMEMS download summary: {len(out_paths)} files "
                f"written to {self.root_dir}"
            )
        else:
            # Reached only for an empty request — total failure raises
            # inside _fetch_with_progress before we get here.
            logger.warning(
                "CMEMS download summary: no datasets requested, nothing written"
            )
        return out_paths

    def _aggregate_outputs(
        self, nc_paths: list[Path], config: AggregationConfig
    ) -> list[Path]:
        """Reduce each subset NetCDF via `pyramids.netcdf.NetCDF.reduce`.

        Maps the :class:`earthlens.aggregate.AggregationConfig` request
        onto the pyramids time-window reducer (shipped as
        `NetCDF.reduce(dim, how, *, groupby, skipna)`): for every path
        in `nc_paths` the depth axis is resolved first (pinned to
        `config.level`, or collapsed by mean when present and no level
        is given), then the `time` axis is windowed by `config.freq`
        with the `config.op` reducer, and the result is written next to
        the source as `<stem>_<freq>_agg.nc`.

        Args:
            nc_paths: The raw subset NetCDFs from `download`.
            config: The aggregation request.

        Returns:
            list[Path]: The per-`(variable, window)` GeoTIFF paths
                across every input NetCDF (skipping non-NetCDF inputs
                such as Zarr stores, which the reducer does not
                handle).

        Raises:
            NotImplementedError: When the installed pyramids exposes no
                `NetCDF.reduce` (the feature ships in a later pyramids
                release; until then call `download()` without
                `aggregate=` and post-process manually).
        """
        from pyramids.netcdf import NetCDF

        if not hasattr(NetCDF, "reduce"):
            raise NotImplementedError(
                "CMEMS.download(aggregate=...) needs pyramids' "
                "NetCDF.reduce(dim, how, groupby=...), which the "
                "installed pyramids build does not provide. Upgrade "
                "pyramids to a release that ships NetCDF.reduce, or "
                "call download() without aggregate= and post-process "
                "the returned NetCDF directly."
            )

        how = "mean" if config.op == "auto" else config.op
        out_paths: list[Path] = []
        for nc_path in nc_paths:
            if nc_path.suffix.lower() != ".nc":
                logger.warning(
                    f"skipping aggregate for non-NetCDF output {nc_path.name!r} "
                    "(the reducer only handles NetCDF)"
                )
                continue
            out_paths.extend(self._aggregate_one(nc_path, config, how))
        return out_paths

    def _aggregate_one(
        self, nc_path: Path, config: AggregationConfig, how: str
    ) -> list[Path]:
        """Reduce one subset NetCDF into per-`(variable, window)` GeoTIFFs.

        Resolves the depth axis (pinned via `config.level` or collapsed
        by mean), windows the `time` axis by `config.freq` using
        :meth:`pyramids.netcdf.NetCDF.reduce`, then writes one GeoTIFF
        per variable per window via
        :meth:`pyramids.dataset.Dataset.from_array` — the same
        proven raster-write path :func:`earthlens.aggregate.aggregate_netcdf`
        uses for the ECMWF backend, so CMEMS aggregate output is shaped
        like ECMWF's (per-window GeoTIFFs, not a multidimensional
        NetCDF, which the GDAL netCDF driver cannot write back).

        The window labels are computed here from the file's CF-decoded
        time axis (pyramids' `NetCDF.get_time_variable`, which parses the
        CF `units` + `calendar`; see :meth:`_window_labels`) and handed to
        `reduce` as an explicit per-timestep label sequence, so each
        output slice carries a start-of-window `YYYYMMDD` label for its
        filename.

        Args:
            nc_path: The NetCDF to reduce.
            config: The aggregation request (provides `freq`, `level`,
                `out_dir`, `skipna`).
            how: The already-resolved reducer (`config.op` with
                `"auto"` mapped to `"mean"`).

        Returns:
            list[Path]: One GeoTIFF path per `(variable, window)`,
                written under `config.out_dir` (or next to `nc_path`
                when `out_dir` is `None`).

        Raises:
            ValueError: When the file has no `time` dimension to window.
        """
        from pyramids.netcdf import NetCDF

        from earthlens.base.raster import array_to_raster

        nc = NetCDF.read_file(str(nc_path))
        dims = tuple(nc.dimension_names or ())
        if "time" not in dims:
            raise ValueError(
                f"{nc_path.name} has no `time` dimension to aggregate (dims={dims})."
            )

        # Compute the window labels from the freshly-read container, before any
        # depth `sel`/`reduce` — depth handling leaves the `time` axis unchanged
        # but may not preserve its CF `units`/`calendar` attributes, and the
        # label count still matches the (unchanged) time dimension afterwards.
        labels = self._window_labels(nc, config.freq)
        windows = list(dict.fromkeys(labels))

        depth_collapsed = False
        if "depth" in dims:
            if config.level is not None:
                nc = nc.sel(depth=config.level)
            else:
                nc = nc.reduce("depth", how="mean", skipna=config.skipna)
                depth_collapsed = True

        reduced = nc.reduce("time", how=how, groupby=labels, skipna=config.skipna)

        out_dir = Path(config.out_dir) if config.out_dir is not None else nc_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for var_name in reduced.variable_names:
            var = reduced.get_variable(var_name)
            arr = var.read_array()
            if arr.ndim == 2:
                arr = arr[None, :, :]
            for i, window in enumerate(windows):
                target = out_dir / (
                    f"{nc_path.stem}_{var_name}_{config.freq}_{window}.tif"
                )
                array_to_raster(arr[i], var.geotransform, epsg=var.epsg).to_file(
                    str(target)
                )
                written.append(target)

        logger.info(
            f"CMEMS aggregate: {nc_path.name} -> {len(written)} GeoTIFF(s) "
            f"({len(windows)} windows x {len(reduced.variable_names)} vars, "
            f"time/{config.freq} {how}"
            f"{', depth collapsed' if depth_collapsed else ''})"
        )
        return written

    @staticmethod
    def _window_labels(nc: NetCDF, freq: str) -> list[str]:
        """Return one window label per timestep, bucketing time by `freq`.

        Reads the NetCDF's CF-decoded time axis from pyramids'
        :meth:`pyramids.netcdf.NetCDF.get_time_variable` (which parses
        the CF `units` + `calendar`), builds a `pandas.DatetimeIndex`,
        then assigns each timestep the start-of-window timestamp of its
        `freq` bucket. Timesteps in the same window share a label, so
        :meth:`pyramids.netcdf.NetCDF.reduce` coarsens `time` to one
        slice per distinct window.

        The axis is requested at second resolution
        (`time_format="%Y-%m-%d %H:%M:%S"`) rather than via the
        date-only `time_stamp` property, so a sub-daily `freq` does not
        collapse intra-day steps into a single bucket.

        Assumes a standard / proleptic-Gregorian calendar (what CMEMS
        ocean products use); the decoded timestamps are parsed with
        `pandas.to_datetime`, which does not handle exotic CF calendars
        (`360_day`, `noleap`).

        Args:
            nc: The NetCDF whose time axis to bucket.
            freq: A pandas offset alias (`"1MS"`, `"7D"`, `"YS"`, …).

        Returns:
            list[str]: One `YYYYMMDD` window label per timestep, in
                file order (length = the time dimension size).

        Raises:
            ValueError: When the CF `time` axis cannot be decoded.
        """
        times = nc.get_time_variable("time", time_format="%Y-%m-%d %H:%M:%S")
        if not times:
            raise ValueError(
                "cannot decode the NetCDF CF `time` axis for windowing "
                "(no `time` variable with a CF `units` attribute)."
            )
        return window_labels(times, freq)

    def _search(self) -> list[RemoteProduct]:
        """One :class:`RemoteProduct` per `(dataset_id, variables)` group.

        CMEMS exposes a single server-side subset endpoint per
        dataset, so there is exactly one product per dataset in
        the user's request. The variable list rides on the
        product's `metadata` dict; no network call is made (the
        toolbox's catalogue lookup happens lazily inside
        `subset()` itself).

        Returns:
            list[RemoteProduct]: One product per dataset id in
                `self.vars`; metadata carries `{"variables":
                [...]}`.
        """
        ext = "nc" if self._file_format == "netcdf" else "zarr"
        filenames = _unique_output_names(list(self.vars), ext)
        products: list[RemoteProduct] = []
        # CMEMS always builds `self.vars` in the {dataset_id: [vars]} form.
        assert isinstance(self.vars, dict)
        for dataset_id, var_codes in self.vars.items():
            products.append(
                RemoteProduct(
                    id=dataset_id,
                    metadata={
                        "variables": list(var_codes),
                        "output_filename": filenames[dataset_id],
                    },
                )
            )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[Path]:
        """Subset each :class:`RemoteProduct` server-side.

        Default progress-bar setting (`disable_progress_bar=False`
        in the toolbox) is overridden by
        :meth:`_api_via_search_fetch_with_progress` when called
        through :meth:`download`; the plain `_api` /
        `_api_via_search_fetch` path leaves the toolbox's default
        in place.

        Args:
            products: The list returned by :meth:`_search` (or any
                user-filtered subset of it).

        Returns:
            list[Path]: One file path per successfully subset
                product. Failures are logged and dropped from the
                result rather than aborting the loop.
        """
        return self._fetch_with_progress(products, progress_bar=True)

    def _api_via_search_fetch_with_progress(self, progress_bar: bool) -> list[Path]:
        """C3 composition with explicit progress-bar control."""
        products = self._search()
        if not products:
            return []
        return self._fetch_with_progress(products, progress_bar=progress_bar)

    def _fetch_with_progress(
        self, products: list[RemoteProduct], progress_bar: bool
    ) -> list[Path]:
        """Subset every product under the caller's partial-failure policy.

        Args:
            products: The products to subset.
            progress_bar: Forwarded to
                `copernicusmarine.subset(disable_progress_bar=...)`.

        Returns:
            list[Path]: Successful output paths.

        Raises:
            RuntimeError: When every product failed, so a caller cannot
                silently process nothing.
        """
        out_paths, failed = self._run_items(
            products,
            partial(self._subset_one, progress_bar=progress_bar),
            errors=self._errors,
            label="subset",
            describe=_describe_product,
        )
        # Partial failure (some products wrote) returns the successes so a
        # multi-dataset request is not all-or-nothing. Total failure raises
        # rather than returning an empty list, so a caller doing
        # `paths = download(); use(paths)` cannot silently process nothing.
        if failed and not out_paths:
            failed_summary = ", ".join(
                f"{ds_id} ({type(exc).__name__})" for ds_id, exc in failed
            )
            raise RuntimeError(
                f"all {len(failed)} CMEMS subset(s) failed: "
                f"{failed_summary}. See the per-product ERROR logs "
                "above for the underlying toolbox exceptions."
            )
        return cast("list[Path]", out_paths)

    def _subset_one(self, product: RemoteProduct, *, progress_bar: bool) -> Path:
        """Submit one `copernicusmarine.subset` request.

        Args:
            product: One :class:`RemoteProduct` from `_search`;
                `product.id` is the CMEMS dataset id and
                `product.metadata["variables"]` is the list of
                variable short names to retrieve.
            progress_bar: Forwarded to the toolbox's
                `disable_progress_bar=` (negated).

        Returns:
            Path: Absolute path of the NetCDF (or Zarr) written
                under `self.root_dir`.

        Raises:
            AuthenticationError: When credentials are rejected
                mid-call (a non-eager rotation).
            FileNotFoundError: When the toolbox returns a success
                response without a writable `file_path` — surfaced
                so callers can distinguish "subset returned
                metadata only" from a normal failure.
        """
        import copernicusmarine as cm

        dataset_id = product.id
        variables = product.metadata.get("variables") or []
        ext = "nc" if self._file_format == "netcdf" else "zarr"
        output_filename = product.metadata.get("output_filename") or (
            f"{safe_filename(dataset_id)}.{ext}"
        )

        logger.info(
            f"Requesting CMEMS subset for {dataset_id!r} "
            f"variables={variables} → {output_filename}"
        )

        try:
            response = cm.subset(
                dataset_id=dataset_id,
                variables=list(variables) or None,
                minimum_longitude=self.space.west,
                maximum_longitude=self.space.east,
                minimum_latitude=self.space.south,
                maximum_latitude=self.space.north,
                minimum_depth=self._minimum_depth,
                maximum_depth=self._maximum_depth,
                start_datetime=str(self.time.start_date),
                end_datetime=str(self.time.end_date),
                output_filename=output_filename,
                output_directory=str(self.root_dir),
                file_format=self._file_format,
                overwrite=self._overwrite,
                disable_progress_bar=not progress_bar,
                credentials_file=(
                    str(self._credentials_file)
                    if self._credentials_file is not None
                    else None
                ),
            )
        except cm.InvalidUsernameOrPassword as exc:
            raise AuthenticationError(
                "Copernicus Marine rejected the credentials mid-request. "
                "Re-authenticate with `copernicusmarine login` or pass "
                "fresh service_username= / service_password= to CMEMS()."
            ) from exc

        file_path = getattr(response, "file_path", None)
        if file_path is None:
            raise FileNotFoundError(
                f"copernicusmarine.subset returned no file_path for "
                f"{dataset_id!r}; response status="
                f"{getattr(response, 'status', None)!r}"
            )
        return Path(file_path)


def _unique_output_names(dataset_ids: list[str], ext: str) -> dict[str, str]:
    """Map each dataset id to a collision-free `<stem>.<ext>` filename.

    :func:`_safe_filename` is many-to-one — two distinct dataset ids
    can normalise to the same stem (e.g. an id with a `.` and one with
    a `_` in the same position). Writing both to the same output
    directory in one `download()` would silently overwrite. This
    builds the per-request filename map up front and, only for stems
    shared by more than one id, disambiguates by appending a short
    deterministic hash of the full dataset id. Non-colliding ids keep
    their clean stem.

    Args:
        dataset_ids: The dataset ids in one `download()` request.
        ext: File extension without the dot (`"nc"` or `"zarr"`).

    Returns:
        Mapping from each dataset id to its output filename. Values
            are unique across the input.

    Examples:
        - Distinct ids that don't normalise-collide keep clean stems:
            ```python
            >>> from earthlens.cmems.backend import _unique_output_names
            >>> _unique_output_names(
            ...     ["cmems_mod_glo_phy_my_0.083deg_P1D-m", "med-cmcc-tem-rean-d"],
            ...     "nc",
            ... )
            {'cmems_mod_glo_phy_my_0.083deg_P1D-m': 'cmems_mod_glo_phy_my_0.083deg_P1D-m.nc', 'med-cmcc-tem-rean-d': 'med-cmcc-tem-rean-d.nc'}

            ```
        - Two ids that normalise to the same stem get distinct names:
            ```python
            >>> from earthlens.cmems.backend import _unique_output_names
            >>> names = _unique_output_names(["a/b", "a_b"], "nc")
            >>> len(set(names.values()))
            2
            >>> all(v.startswith("a_b_") and v.endswith(".nc") for v in names.values())
            True

            ```
        - The extension is applied and empty input maps to `{}`:
            ```python
            >>> from earthlens.cmems.backend import _unique_output_names
            >>> _unique_output_names(["ds-1"], "zarr")
            {'ds-1': 'ds-1.zarr'}
            >>> _unique_output_names([], "nc")
            {}

            ```
    """
    import hashlib

    stems: dict[str, str] = {ds_id: safe_filename(ds_id) for ds_id in dataset_ids}
    counts = Counter(stems.values())
    names: dict[str, str] = {}
    for ds_id, stem in stems.items():
        if counts[stem] > 1:
            digest = hashlib.blake2b(ds_id.encode("utf-8"), digest_size=4).hexdigest()
            stem = f"{stem}_{digest}"
        names[ds_id] = f"{stem}.{ext}"
    return names
