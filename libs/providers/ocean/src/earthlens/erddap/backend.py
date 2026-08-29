"""Generic ERDDAP backend — `ERDDAP(AbstractDataSource)`.

Reaches any public ERDDAP server from one class. A `dataset=<id>`
selects a curated :class:`~earthlens.erddap.catalog.Dataset` row
(`server_url` + `dataset_id` + `protocol`); the row's `protocol` decides
the per-instance :attr:`ERDDAP.OUTPUT_KIND` and the realisation path:

* **`tabledap`** → `OUTPUT_KIND = "tabular"`. The request is shaped with
  the **`erddapy`** SDK and realised through `ERDDAP.to_pandas()`, so the
  result is a :class:`pandas.DataFrame`. The
  :class:`earthlens.earthlens.EarthLens` facade rejects `aggregate=` for
  it (a table has no gridded reduction).
* **`griddap`** → `OUTPUT_KIND = "raster"`. The OPeNDAP `.nc` download
  URL is built directly (see :func:`earthlens.erddap._helpers.build_griddap_url`)
  and fetched with a plain HTTP GET — deliberately **not** through the
  erddapy instance, whose `dataset_id` setter eagerly fetches the full
  coordinate axis from the server (a slow / hanging metadata call). The
  written NetCDF is read back only via **pyramids** when an `aggregate=`
  reduction is requested, so earthlens never imports `xarray`.

An empty result is handled **asymmetrically by design**: a tabledap
query that matches no rows returns an empty frame plus a warning (a table
of zero rows is a valid, recoverable answer), whereas a griddap request
outside the dataset's coverage raises a clear `ValueError` (a raster has
no "zero pixels" — an out-of-coverage grid request is an error). A script
moved from a tabledap to a griddap dataset should expect the same empty
query to hard-fail rather than return empty.

Only public (no-auth) servers ship in the catalog, so there is no auth
module.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
)
from earthlens.base.http import HttpClient
from earthlens.erddap._helpers import (
    build_constraints,
    build_griddap_url,
    empty_canonical,
)
from earthlens.erddap.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig
    from earthlens.ecmwf import Variable

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk table formats for a tabledap result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Distinctive phrase ERDDAP returns in its 404 body when a tabledap query
#: matches no rows (`"Your query produced no matching results."`). Matched
#: case-insensitively, and only alongside a 404 status when one is available
#: (erddapy re-raises a bare `HTTPError` with no `response`, so the status is
#: usually absent — the phrase then decides on its own).
_NO_MATCH_MARKER = "produced no matching results"

#: Leading magic bytes of the NetCDF container formats ERDDAP serves —
#: classic NetCDF-3 (`CDF\x01/02/05`) and NetCDF-4/HDF5 (`\x89HDF`). A griddap
#: body that does not start with one of these is an error page (ERDDAP serves
#: those as HTML, sometimes with a 200), not data.
_NETCDF_MAGIC: tuple[bytes, ...] = (b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF")


@dataclass(frozen=True)
class _GridVarInfo:
    """Minimal `var_info` adapter for :func:`earthlens.aggregate.aggregate_netcdf`.

    The aggregator only reads three fields off the catalog row it is
    handed — the in-NetCDF variable name, the output-filename seed, and
    the flux marker. ERDDAP griddap variables are named identically in
    the request and the file. `is_flux` is set per variable from the
    resolved dataset's `flux_variables` list: it is `True` for an
    accumulation/flux variable (→ `op="auto"` resolves to `"sum"`) and
    `False` for an instantaneous state field (→ `"mean"`). The shipped
    rows declare no fluxes (SST anomaly, DHW, chlorophyll are all state),
    so the default is `False`.

    Attributes:
        nc_variable: Variable name inside the downloaded NetCDF.
        cds_variable: Seeds the aggregated output filename.
        is_flux: `True` for a flux/accumulation variable (`op="auto"` →
            `"sum"`), `False` for a state field (`"mean"`).
    """

    nc_variable: str
    cds_variable: str
    is_flux: bool = False


class ERDDAP(AbstractDataSource):
    """Generic ERDDAP-server backend (per-instance raster / tabular output).

    Fetches a curated dataset from any public ERDDAP server through the
    uniform `download()` shape. The selected `dataset=`'s `protocol`
    fixes the output: `griddap` writes raster NetCDF (and accepts an
    `aggregate=` pyramids reduction), `tabledap` returns a long
    :class:`pandas.DataFrame`. The query is a search/fetch split:
    :meth:`_search` names the single resolved product and :meth:`_fetch`
    realises it.

    Attributes:
        OUTPUT_KIND: Set **per instance** in :meth:`__init__` from the
            resolved dataset's `protocol` (`griddap` → `"raster"`,
            `tabledap` → `"tabular"`) — the sanctioned earthdata /
            eumetsat override. The facade reads it to gate `aggregate=`.

    Examples:
        - A griddap dataset writes raster NetCDF (marked `+SKIP` — it hits
          the live NOAA CoastWatch server):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> paths = EarthLens(  # doctest: +SKIP
            ...     data_source="erddap",
            ...     dataset="NOAA_DHW",
            ...     variables=["CRW_SSTANOMALY"],
            ...     start="2023-06-01",
            ...     end="2023-06-01",
            ...     lat_lim=[0.0, 5.0],
            ...     lon_lim=[150.0, 155.0],
            ...     path="erddap_out",
            ... ).download()  # -> [Path('erddap_out/NOAA_DHW.nc')]

            ```
        - A tabledap dataset returns a `pandas.DataFrame` (also `+SKIP`):

            ```python
            >>> from earthlens.earthlens import EarthLens
            >>> df = EarthLens(  # doctest: +SKIP
            ...     data_source="erddap",
            ...     dataset="cwwcNDBCMet",
            ...     variables=["station", "time", "WTMP"],
            ...     start="2023-01-01",
            ...     end="2023-01-02",
            ...     lat_lim=[36.0, 37.0],
            ...     lon_lim=[-123.0, -122.0],
            ...     path="erddap_out",
            ... ).download()

            ```
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    AGGREGATE_REFUSAL_REASON = (
        "a tabledap response is tabular; only griddap grids have an axis to reduce"
    )

    def __init__(
        self,
        start: str,
        end: str,
        lat_lim: list[float],
        lon_lim: list[float],
        dataset: str = "",
        variables: list[str] | None = None,
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        output_format: OutputFormat = "csv",
        timeout: float = 120.0,
    ):
        """Initialise an ERDDAP backend instance.

        Resolves `dataset=` against the catalog and sets the per-instance
        :attr:`OUTPUT_KIND` **before** calling the parent constructor, so
        the facade sees the right kind when it gates `aggregate=`.

        Args:
            start: Inclusive start of the window, parsed with `fmt`.
            end: Inclusive end of the window.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes.
            dataset: The curated ERDDAP dataset id to fetch (a key in the
                bundled catalog, e.g. `"NOAA_DHW"`). Required.
            variables: Variable / column names to request. An empty value
                falls back to the catalog row's default set.
            temporal_resolution: Resolution label recorded on the
                temporal extent (ERDDAP returns the dataset's native
                cadence regardless).
            path: Output directory for the written NetCDF / table.
            fmt: `strptime` format for `start` / `end`.
            output_format: On-disk format for a tabledap result — `"csv"`
                (default) or `"parquet"`. Ignored for griddap.
            timeout: Per-request HTTP timeout in seconds for the griddap
                download.

        Raises:
            ValueError: If `dataset` is empty, unknown (the catalog's
                did-you-mean is surfaced), or `output_format` is invalid.
            TypeError: If `variables` is a mapping (ERDDAP takes a flat
                list of names; the dataset is named by `dataset=`).
        """
        if not dataset:
            raise ValueError(
                "ERDDAP requires dataset=<id> naming a curated catalog row "
                "(e.g. dataset='NOAA_DHW'). List ids with "
                "earthlens.erddap.Catalog().datasets."
            )
        if isinstance(variables, dict):
            raise TypeError(
                "ERDDAP `variables` must be a list of variable/column names "
                "(e.g. ['CRW_SSTANOMALY']), not a mapping. Name the dataset "
                "with dataset=<id> instead."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        self._catalog = Catalog()
        self._dataset: Dataset = self._catalog.get(dataset)
        self.OUTPUT_KIND = (
            "raster" if self._dataset.protocol == "griddap" else "tabular"
        )
        self._output_format: OutputFormat = output_format
        self._timeout = timeout

        resolved_variables = (
            list(variables) if variables else list(self._dataset.variables)
        )
        super().__init__(
            start=start,
            end=end,
            variables=resolved_variables,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        The whole window is sent to ERDDAP in one request, so `dates`
        collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        return self._whole_window_extent(
            start, end, fmt=fmt, resolution=temporal_resolution
        )

    def _search(self) -> list[RemoteProduct]:
        """Name the single resolved product (one catalog row per request).

        Returns:
            list[RemoteProduct]: One product carrying the resolved
                :class:`Dataset` row in its `metadata`.
        """
        return [
            RemoteProduct(
                id=self._dataset.dataset_id,
                metadata={"dataset": self._dataset},
            )
        ]

    def _fetch(self, products: list[RemoteProduct]) -> list:
        """Realise the request — a `DataFrame` (tabledap) or a `Path` (griddap).

        Widens the inherited `-> list[Path]` contract: a tabledap fetch
        returns an in-memory frame (the write happens in :meth:`download`
        via :meth:`_write_table`), a griddap fetch returns the written
        NetCDF path.

        Deliberately takes no `limit=`, unlike the other tabular backends.
        `_search` resolves to a single dataset, so there is no per-product loop
        a cap could stop: griddap streams one file, and tabledap is one request
        whose rows have all been transferred by the time a frame exists. A
        client-side cap here would trim the result without saving any work —
        the decorative form `TestLimitIsBoundedNotTrimmed` exists to catch. The
        real bound is ERDDAP's own server-side `orderByLimit`, which belongs in
        `build_constraints` and needs verifying against a live server before it
        is claimed; narrowing the date window is the bound available today.

        Args:
            products: The single-element list from :meth:`_search`.

        Returns:
            list: `[pandas.DataFrame]` for tabledap, `[Path]` for griddap.
        """
        row = self._dataset
        variables = list(self.vars)
        if row.protocol == "tabledap":
            return [self._fetch_table(row, variables)]
        return [self._fetch_grid(row, variables)]

    def _fetch_table(self, row: Dataset, variables: list[str]) -> pd.DataFrame:
        """Fetch a tabledap dataset to a `DataFrame` via erddapy.

        Args:
            row: The resolved tabledap catalog row.
            variables: Column names to request.

        Returns:
            pd.DataFrame: The result. A populated frame uses ERDDAP's
                column names, which carry units (e.g. `"WTMP (degree_C)"`);
                the empty-match fallback instead uses the **bare requested
                names** (`"WTMP"`), since the unit-suffixed names are only
                known from a successful response. So `df.columns` differs
                between a matched and an empty result.

        Raises:
            requests.exceptions.HTTPError: For any HTTP failure other than
                the empty-result 404.
        """
        from erddapy import ERDDAP as _ErddapClient

        client = _ErddapClient(server=row.server_url, protocol="tabledap")
        client.dataset_id = row.dataset_id
        client.variables = variables
        client.constraints = build_constraints(
            self.space, self.time, "tabledap", lon_360=row.lon_360
        )
        try:
            return client.to_pandas()
        except requests.exceptions.HTTPError as exc:
            # ERDDAP signals "no rows" with a 404 whose body carries the
            # marker phrase. Gate on the phrase, and additionally on a 404
            # when a status is present, so a 500/403 error page that merely
            # mentions the phrase is not silently downgraded to "empty".
            status = getattr(exc.response, "status_code", None)
            if _NO_MATCH_MARKER in str(exc).lower() and status in (None, 404):
                extent = self._extent_label()
                logger.warning(
                    f"ERDDAP tabledap {row.dataset_id}: no rows over {extent}; "
                    f"returning an empty frame."
                )
                warnings.warn(
                    f"ERDDAP query for {row.dataset_id!r} matched no rows over "
                    f"{extent}.",
                    stacklevel=2,
                )
                return empty_canonical(variables)
            raise

    def _client(self) -> HttpClient:
        """Return this instance's HTTP client, built once.

        Held on the instance so a multi-dataset request reuses one pooled
        connection to the ERDDAP host rather than re-handshaking per row.

        Returns:
            HttpClient: The shared client.
        """
        if self._http is None:
            self._http = HttpClient(
                timeout=self._timeout,
                max_retries=0,
                status_forcelist=(),
                raise_for_status=True,
            )
        return self._http

    def _fetch_grid(self, row: Dataset, variables: list[str]) -> Path:
        """Stream a griddap subset to a `.nc` file and return its path.

        Builds the OPeNDAP URL directly (avoiding erddapy's axis-fetch)
        and streams the response to disk in blocks — a griddap window can
        run to gigabytes, so holding the body in memory to then write it
        would double the peak footprint. The leading bytes are checked
        against the NetCDF magic before the file is published, so an HTML
        error page (which ERDDAP sometimes serves with a `200`) never lands
        as a `.nc`. An out-of-coverage / no-data response surfaces as a
        clear :class:`ValueError` naming the dataset and bbox, not a bare
        HTTP stack trace.

        Args:
            row: The resolved griddap catalog row.
            variables: Grid variable names to request.

        Returns:
            Path: The written NetCDF at `<root_dir>/<dataset_id>.nc`.

        Raises:
            ValueError: When the ERDDAP server rejects the request, or
                returns a non-NetCDF body (an HTML error page, sometimes
                served with a 200) — e.g. the bbox/time is outside the
                dataset's coverage.
        """
        constraints = build_constraints(self.space, self.time, "griddap")
        url = build_griddap_url(
            row.server_url, row.dataset_id, variables, row.dim_names, constraints
        )
        dest = self.root_dir / f"{row.dataset_id}.nc"
        logger.info(f"ERDDAP griddap {row.dataset_id}: GET {url}")
        http = self._client()
        # Stream to disk rather than materialising `response.content`: a
        # griddap window can run to gigabytes, and holding the whole body in
        # memory to then write it doubles the peak footprint for no gain.
        #
        # ERDDAP does not uniformly use 4xx/5xx for griddap problems: an
        # out-of-coverage request, a maintenance notice, or a proxy
        # interstitial can arrive as a 200 with an HTML body. Writing that
        # to `<id>.nc` would yield a corrupt file that only fails much later
        # (in pyramids or the user's own read), so `expect_magic` validates
        # the leading bytes and discards the partial write instead.
        try:
            http.download(url, dest, progress=False, expect_magic=_NETCDF_MAGIC)
        except requests.exceptions.HTTPError as exc:
            raise ValueError(
                f"ERDDAP griddap request for {row.dataset_id!r} failed over "
                f"{self._extent_label()}: {exc}. The window may be outside "
                f"the dataset's coverage."
            ) from exc
        except ValueError as exc:
            raise ValueError(
                f"ERDDAP griddap {row.dataset_id!r} returned a non-NetCDF body "
                f"over {self._extent_label()}: {exc} The bbox/time may be "
                f"outside the dataset's coverage."
            ) from exc
        return dest

    def _extent_label(self) -> str:
        """Render the request bbox + window for error / warning messages."""
        return (
            f"bbox [{self.space.west}, {self.space.south}, "
            f"{self.space.east}, {self.space.north}] / "
            f"[{self.time.start_date:%Y-%m-%d}..{self.time.end_date:%Y-%m-%d}]"
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> pd.DataFrame | list[Path]:
        """Fetch the dataset and return its artifact (frame or NetCDF paths).

        Args:
            progress_bar: Accepted for signature parity; ERDDAP issues a
                single request per call, so there is no per-item bar.
            aggregate: Optional :class:`~earthlens.aggregate.AggregationConfig`.
                Accepted **only** for a griddap (raster) dataset, where
                every downloaded NetCDF is reduced through
                :func:`earthlens.aggregate.aggregate_netcdf` (the pyramids
                flow ECMWF uses); when the config's `out_dir` is `None` it
                defaults to `<root_dir>/aggregated/`. For a tabledap
                (tabular) dataset a non-`None` `aggregate` raises
                `NotImplementedError` (the facade already gates this; this
                is the belt-and-suspenders guard for direct callers).

        Returns:
            pandas.DataFrame: For a tabledap dataset (also written to disk
                as CSV / Parquet).
            list[Path]: For a griddap dataset — the written `.nc`
                path(s), or the aggregated GeoTIFFs when `aggregate=` is
                passed.

        Raises:
            NotImplementedError: If `aggregate` is not `None` for a
                tabledap dataset.
            ValueError: If a griddap request is outside the dataset's
                coverage (from :meth:`_fetch_grid`).
        """
        if self.OUTPUT_KIND == "tabular":
            frames = self._api()
            df = (
                pd.concat(frames, ignore_index=True)
                if frames
                else empty_canonical(list(self.vars))
            )
            out_path = self._write_table(df)
            # A no-match is already warned (with the extent) in `_fetch_table`;
            # here just record where the table landed, at the matching level.
            level = logger.info if len(df) else logger.debug
            level(
                f"ERDDAP {self._dataset.dataset_id}: {len(df)} row(s) written "
                f"to {out_path}"
            )
            return df

        nc_paths: list[Path] = self._api()
        if aggregate is None:
            return nc_paths
        return self._aggregate(nc_paths, aggregate)

    def _aggregate(
        self, nc_paths: list[Path], aggregate: AggregationConfig
    ) -> list[Path]:
        """Reduce each downloaded griddap NetCDF through pyramids.

        Mirrors the ECMWF aggregate flow: default the config's `out_dir`
        to `<root_dir>/aggregated/`, then run
        :func:`earthlens.aggregate.aggregate_netcdf` per requested
        variable on each NetCDF.

        Args:
            nc_paths: The written `.nc` paths from :meth:`_fetch_grid`.
            aggregate: The caller's aggregation config.

        Returns:
            list[Path]: The written aggregated GeoTIFFs.
        """
        from earthlens.aggregate import aggregate_netcdf

        effective = aggregate
        if aggregate.out_dir is None:
            effective = aggregate.model_copy(
                update={"out_dir": self.root_dir / "aggregated"}
            )
        flux = set(self._dataset.flux_variables)
        out_paths: list[Path] = []
        for nc_path in nc_paths:
            for var in self.vars:
                var_info = _GridVarInfo(
                    nc_variable=var, cds_variable=var, is_flux=var in flux
                )
                agg = aggregate_netcdf(nc_path, cast("Variable", var_info), effective)
                out_paths.extend(p for _, _, p in agg if p is not None)
        return out_paths

    def _write_table(self, df: pd.DataFrame) -> Path:
        """Write a tabledap frame to `root_dir` and return the path.

        Args:
            df: The result frame.

        Returns:
            Path: The written CSV / Parquet file path.

        Raises:
            ImportError: If `output_format="parquet"` but `pyarrow` is
                not installed.
        """
        ext = "parquet" if self._output_format == "parquet" else "csv"
        out_path = self.root_dir / f"{self._dataset.dataset_id}.{ext}"
        if self._output_format == "parquet":
            try:
                df.to_parquet(out_path, index=False)
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "Writing Parquet requires 'pyarrow'. Install it (pip "
                    "install pyarrow) or use output_format='csv'."
                ) from exc
        else:
            df.to_csv(out_path, index=False)
        return out_path
