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

Only public (no-auth) servers ship in the catalog, so there is no auth
module.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
import requests
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.erddap._helpers import (
    build_constraints,
    build_griddap_url,
    empty_canonical,
)
from earthlens.erddap.catalog import Catalog, Dataset

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

OutputFormat = Literal["csv", "parquet"]

#: Accepted on-disk table formats for a tabledap result.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Substring ERDDAP returns in the 404 body when a query matches nothing.
_NO_MATCH_MARKER = "no matching results"


@dataclass(frozen=True)
class _GridVarInfo:
    """Minimal `var_info` adapter for :func:`earthlens.aggregate.aggregate_netcdf`.

    The aggregator only reads three fields off the catalog row it is
    handed — the in-NetCDF variable name, the output-filename seed, and
    the flux marker. ERDDAP griddap variables are named identically in
    the request and the file, and are overwhelmingly instantaneous
    *state* fields (SST anomaly, DHW, chlorophyll), so `is_flux` defaults
    to `False` (→ `op="auto"` resolves to `"mean"`). Pass an explicit
    `op="sum"` to :class:`~earthlens.aggregate.AggregationConfig` for a
    genuine accumulation variable.

    Attributes:
        nc_variable: Variable name inside the downloaded NetCDF.
        cds_variable: Seeds the aggregated output filename.
        is_flux: `False` (state) by default; the aggregator maps it to a
            `"mean"` reducer under `op="auto"`.
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
    """

    OUTPUT_KIND: OutputKind = "raster"

    def __init__(
        self,
        start: str,
        end: str,
        lat_lim: list[float],
        lon_lim: list[float],
        dataset: str = "",
        variables: list[str] | None = None,
        temporal_resolution: str = "daily",
        path: Path | str = "",
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

        resolved_variables = list(variables) if variables else list(
            self._dataset.variables
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

    def _initialize(self):
        """No client / auth — the shipped servers are public (returns `None`)."""
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

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
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        import datetime as _dt

        start_dt = _dt.datetime.strptime(start, fmt)
        end_dt = _dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _api(self) -> list:
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()

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
            pd.DataFrame: The result, or an empty canonical frame (the
                requested columns, no rows) when the query matched nothing.

        Raises:
            requests.exceptions.HTTPError: For any HTTP failure other than
                the empty-result 404.
        """
        from erddapy import ERDDAP as _ErddapClient

        client = _ErddapClient(server=row.server_url, protocol="tabledap")
        client.dataset_id = row.dataset_id
        client.variables = variables
        client.constraints = build_constraints(self.space, self.time, "tabledap")
        try:
            return client.to_pandas()
        except requests.exceptions.HTTPError as exc:
            if _NO_MATCH_MARKER in str(exc).lower():
                logger.warning(
                    f"ERDDAP tabledap {row.dataset_id}: no rows matched the "
                    f"bbox/time window; returning an empty frame."
                )
                warnings.warn(
                    f"ERDDAP query for {row.dataset_id!r} matched no rows.",
                    stacklevel=2,
                )
                return empty_canonical(variables)
            raise

    def _fetch_grid(self, row: Dataset, variables: list[str]) -> Path:
        """Download a griddap subset to a `.nc` file and return its path.

        Builds the OPeNDAP URL directly (avoiding erddapy's axis-fetch)
        and GETs it. An out-of-coverage / no-data response surfaces as a
        clear :class:`ValueError` naming the dataset and bbox, not a bare
        HTTP stack trace.

        Args:
            row: The resolved griddap catalog row.
            variables: Grid variable names to request.

        Returns:
            Path: The written NetCDF at `<root_dir>/<dataset_id>.nc`.

        Raises:
            ValueError: When the ERDDAP server rejects the request (e.g.
                the bbox/time is outside the dataset's coverage).
        """
        constraints = build_constraints(self.space, self.time, "griddap")
        url = build_griddap_url(
            row.server_url, row.dataset_id, variables, row.dim_names, constraints
        )
        dest = self.root_dir / f"{row.dataset_id}.nc"
        logger.info(f"ERDDAP griddap {row.dataset_id}: GET {url}")
        try:
            response = requests.get(url, timeout=self._timeout)
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            raise ValueError(
                f"ERDDAP griddap request for {row.dataset_id!r} failed over "
                f"bbox [{self.space.west}, {self.space.south}, "
                f"{self.space.east}, {self.space.north}] / "
                f"[{self.time.start_date:%Y-%m-%d}..{self.time.end_date:%Y-%m-%d}]: "
                f"{exc}. The window may be outside the dataset's coverage."
            ) from exc
        dest.write_bytes(response.content)
        return dest

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
            if aggregate is not None:
                raise NotImplementedError(
                    "ERDDAP.download(aggregate=...) is not supported for a "
                    "tabledap dataset: its output is a per-row table, not a "
                    "gridded raster, so there is no meaningful gridded "
                    "reduction. Use a griddap dataset (or the CMEMS backend) "
                    "for gridded fields you want to aggregate."
                )
            frames = self._api()
            df = (
                pd.concat(frames, ignore_index=True)
                if frames
                else empty_canonical(list(self.vars))
            )
            out_path = self._write_table(df)
            if len(df):
                logger.info(
                    f"ERDDAP {self._dataset.dataset_id}: {len(df)} row(s) "
                    f"written to {out_path}"
                )
            else:
                logger.warning(
                    f"ERDDAP {self._dataset.dataset_id}: no rows matched; "
                    f"wrote an empty table to {out_path}"
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
        out_paths: list[Path] = []
        for nc_path in nc_paths:
            for var in self.vars:
                var_info = _GridVarInfo(nc_variable=var, cds_variable=var)
                agg = aggregate_netcdf(nc_path, var_info, effective)
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
