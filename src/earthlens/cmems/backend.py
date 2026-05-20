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
backend's `download()` without rejecting it. The aggregator hand-off
itself is staged: the existing
:func:`earthlens.aggregate.aggregate_netcdf` (pyramids-backed via
:class:`pyramids.netcdf.NetCDF`) is hardcoded to consume the ECMWF
`Variable` row shape and a `time × lat × lon` layout. CMEMS NetCDFs
add a depth (or elevation) axis on physics / biogeochem variables,
and CMEMS catalog rows are a different pydantic type. Both gaps
are pyramids-side concerns and will be lifted by generalising the
pyramids time-window reducer to (a) accept any `Variable` exposing
`(nc_variable, output_label, is_flux)` and (b) handle the optional
depth axis (collapse via mean, pick a single level, or preserve
through the per-window write).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.cmems.auth import (
    AuthenticationError,
    CmemsAuth,
    CmemsCredentials,
)

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig


FileFormat = Literal["netcdf", "zarr"]


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
            variable NetCDF output. Composes with the existing
            pyramids-backed
            :class:`earthlens.aggregate.AggregationConfig` flow once
            the pyramids time-window reducer is generalised to
            accept the CMEMS catalog row shape and the optional
            depth axis.
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        variables: dict[str, list[str]],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
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
            temporal_resolution: Advisory cadence label; CMEMS
                handles cadence server-side, so any value the
                source dataset supports is accepted. Defaults to
                `"daily"`.
            path: Output directory. Created by the parent class if
                it does not exist. Defaults to the current working
                directory.
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
        """Build the :class:`CmemsAuth` and run `configure()`.

        Returns `None` so the parent class does not bind any
        opaque object to `self.client` — the toolbox has no
        per-instance client; credentials live in a config file that
        every subsequent `copernicusmarine.subset()` call reads.

        Raises:
            AuthenticationError: When :meth:`CmemsAuth.configure`
                fails (no credentials, rejected credentials, auth
                server unreachable).
        """
        creds = CmemsCredentials(
            username=self._service_username,
            password=self._service_password,
            credentials_file=self._credentials_file,
        )
        self._auth = CmemsAuth(creds)
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Validate and wrap the user bbox into a :class:`SpatialExtent`.

        CMEMS does not impose a global native grid (each dataset
        has its own cell size — 1/12°, 1/4°, 5 km, 2.5 km, …) and
        the toolbox handles snapping server-side, so this method
        does not snap the input box. It is kept as a thin wrapper
        for `SpatialExtent.from_pairs` so the bbox lands on
        `self.space` via the same path the other backends use.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            temporal_resolution: Advisory cadence label; only the
                `"daily"` and `"monthly"` aliases are mapped to a
                pandas frequency, otherwise `freq=None` is used and
                `dates` collapses to the two endpoints.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen pydantic model with parsed bounds.

        Raises:
            ValueError: If `start` parses to a date later than
                `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        freq_map = {"daily": "D", "monthly": "MS", "hourly": "h"}
        resolution = freq_map.get(temporal_resolution, "D")
        dates = pd.date_range(start_dt, end_dt, freq=resolution)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=dates,
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
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
                so the facade allows the kwarg through. Currently
                staged — the call raises `NotImplementedError`
                because the existing pyramids-backed
                :func:`earthlens.aggregate.aggregate_netcdf` is
                hardcoded to the ECMWF `Variable` shape and a
                `time × lat × lon` layout, neither of which fits
                CMEMS rows (different pydantic type, optional
                depth axis). The fix is a pyramids-side
                generalisation of the time-window reducer.

        Returns:
            list[Path]: Absolute paths of every output the
                toolbox successfully wrote. Order matches the
                iteration order of `self.vars`. On a partial
                failure (some pairs succeed, some fail) only the
                successes are returned and the failures are logged;
                an empty list is returned only for an empty request
                (`self.vars == {}`).

        Raises:
            NotImplementedError: When `aggregate` is not `None`.
                Will be removed once the pyramids time-window
                reducer accepts CMEMS catalog rows and a depth
                axis.
            RuntimeError: When **every** `(dataset_id, variables)`
                pair fails its subset (total failure). Raised rather
                than returning `[]` so a caller cannot silently
                process nothing; the message aggregates the failed
                dataset ids and exception types, and the per-product
                toolbox exceptions are logged at ERROR.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "CMEMS.download(aggregate=...) is staged but not "
                "yet implemented. The facade-level OUTPUT_KIND "
                "guard allows aggregate= for raster backends; the "
                "blocker is that earthlens.aggregate.aggregate_netcdf "
                "(backed by pyramids.netcdf.NetCDF) is hardcoded to "
                "the ECMWF Variable shape and a time x lat x lon "
                "layout. CMEMS uses a different catalog row type and "
                "adds an optional depth axis. The fix is a pyramids-"
                "side generalisation of the time-window reducer. "
                "For now, call download() without aggregate= and "
                "post-process the returned NetCDF directly via "
                "pyramids.netcdf.NetCDF."
            )

        out_paths = self._api_via_search_fetch_with_progress(progress_bar)

        if out_paths:
            logger.info(
                f"CMEMS download summary: {len(out_paths)} files "
                f"written to {self.root_dir}"
            )
        else:
            # Reached only for an empty request — total failure raises
            # inside _fetch_with_progress before we get here.
            logger.warning(
                "CMEMS download summary: no datasets requested, "
                "nothing written"
            )
        return out_paths

    def _api(self) -> list[Path]:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

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

    def _api_via_search_fetch_with_progress(
        self, progress_bar: bool
    ) -> list[Path]:
        """C3 composition with explicit progress-bar control."""
        products = self._search()
        if not products:
            return []
        return self._fetch_with_progress(products, progress_bar=progress_bar)

    def _fetch_with_progress(
        self, products: list[RemoteProduct], progress_bar: bool
    ) -> list[Path]:
        """Subset every product, logging per-product failures.

        Args:
            products: The products to subset.
            progress_bar: Forwarded to
                `copernicusmarine.subset(disable_progress_bar=...)`.

        Returns:
            list[Path]: Successful output paths.
        """
        out_paths: list[Path] = []
        failed: list[tuple[str, BaseException]] = []
        for product in products:
            try:
                out_paths.append(self._subset_one(product, progress_bar))
            except Exception as exc:  # noqa: BLE001 - log + continue
                logger.error(
                    f"CMEMS subset for {product.id!r} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                failed.append((product.id, exc))
        if failed:
            failed_summary = ", ".join(
                f"{ds_id} ({type(exc).__name__})" for ds_id, exc in failed
            )
            logger.warning(
                f"{len(failed)} CMEMS subset(s) failed: {failed_summary}"
            )
            # Partial failure (some products wrote) returns the successes
            # so a multi-dataset request is not all-or-nothing. Total
            # failure raises rather than returning an empty list, so a
            # caller doing `paths = download(); use(paths)` cannot
            # silently process nothing.
            if not out_paths:
                raise RuntimeError(
                    f"all {len(failed)} CMEMS subset(s) failed: "
                    f"{failed_summary}. See the per-product ERROR logs "
                    "above for the underlying toolbox exceptions."
                )
        return out_paths

    def _subset_one(
        self, product: RemoteProduct, progress_bar: bool
    ) -> Path:
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
            f"{_safe_filename(dataset_id)}.{ext}"
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


def _safe_filename(dataset_id: str) -> str:
    """Replace characters illegal in Windows filenames with `_`.

    CMEMS dataset ids contain `.` (e.g.
    `cmems_mod_glo_phy_my_0.083deg_P1D-m`); Windows tolerates a
    single dot before the extension but mid-string dots are
    confusing in `glob` patterns. Also strip any path separator
    that could escape the output directory.

    Args:
        dataset_id: The raw CMEMS dataset id.

    Returns:
        A filename-safe variant of the id.
    """
    safe = dataset_id
    for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        safe = safe.replace(bad, "_")
    return safe


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
    """
    import hashlib

    stems: dict[str, str] = {ds_id: _safe_filename(ds_id) for ds_id in dataset_ids}
    counts = Counter(stems.values())
    names: dict[str, str] = {}
    for ds_id, stem in stems.items():
        if counts[stem] > 1:
            digest = hashlib.blake2b(ds_id.encode("utf-8"), digest_size=4).hexdigest()
            stem = f"{stem}_{digest}"
        names[ds_id] = f"{stem}.{ext}"
    return names
