"""CHIRPS / CHIRP / CHIRTS / CHIRPS-GEFS / SPI / SPEI / WBGT / CHPclim FTP backend.

Downloads raster products from the Climate Hazards Center FTP server
(`data.chc.ucsb.edu`) over anonymous FTP. Every dataset's FTP layout,
spatial / temporal extent, file pattern, available formats, and per-
variable metadata is sourced from
`Catalog`, which loads the bundled per-family
`catalog/*.yaml` files. No FTP path or filename is hardcoded here.

The download pipeline (per `(dataset, variable, date)` triple) is:

1. `_api` — resolve the remote directory + filename from
   `Dataset.ftp_bases` / `Dataset.file_patterns` after substituting the
   per-date placeholders (`{year}`, `{month}`, `{day}`, `{dekad}`,
   `{pentad}`, `{hour}`, `{doy}`), fetch the file via FTP, and clip it
   to the user's bbox.
2. `_post_process` — ungzip (when the format is `.gz`), read the
   raster with `pyramids.Dataset`, clip to the bbox using the
   dataset's own geo-affine (no hardcoded 0.05° grid assumption), and
   write the canonical clipped GeoTIFF.

The `variables` constructor argument accepts two shapes:

* `list[str]` — legacy CHIRPS-2.0 list-of-variables. The dataset key
  is derived from `temporal_resolution` via
  `_LEGACY_DATASET_KEY` (`"daily"` → `"global-daily"`,
  `"monthly"` → `"global-monthly"`).
* `dict[str, list[str]]` — mapping of CHIRPS dataset key to a list of
  variable codes, e.g.
  `{"africa-pentad": ["precipitation"]}`. This is the
  ECMWF-style shape and unlocks the full ~100-dataset catalog.
"""

from __future__ import annotations

import math
from contextlib import closing
from ftplib import FTP  # nosec B402  # noqa: S402
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from loguru import logger
from pyramids._io import extract_from_gz
from pyramids.dataset import Dataset, GeoReference
from tqdm import tqdm

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
    close_quietly,
    crop_to_aoi,
    date_windows,
    to_datetime,
)
from earthlens.chc.catalog import Catalog, Variable
from earthlens.chc.catalog import Dataset as ChcDataset

__all__ = ["CHIRPS"]


# Map the legacy `temporal_resolution` shorthand to a CHIRPS dataset key
# so list-shape calls keep working without a breaking change.
# Pre-catalog the only supported shapes were "daily" and "monthly" over
# CHIRPS-2.0 global ±50°.
_LEGACY_DATASET_KEY: dict[str, str] = {
    "daily": "global-daily",
    "monthly": "global-monthly",
}


# Calendar dekad/pentad period boundaries.
#
# CHC publishes dekadal and pentadal products on a calendar grid that
# *resets every month*: a dekad is a 10-day third of the month (days
# 1-10, 11-20, 21-end) and a pentad a 5-day sixth (days 1-5, 6-10,
# 11-15, 16-20, 21-25, 26-end). The last period of each month is short
# -- 8-11 days for the dekad, 3-6 for the pentad -- because it absorbs
# the 28-31 day remainder.
#
# This is why these cadences cannot be walked with a fixed `pandas_freq`
# day stride (`10D` / `5D`): `pd.date_range(..., freq="10D")` never
# realigns to the month boundary, so over a year it drifts off the
# month-reset `{dekad}` / `{pentad}` index -- silently missing one
# period (e.g. February's short third dekad) and re-fetching another.
# `_calendar_period_index` steps on the calendar grid instead. Only the
# `dekadal` / `pentadal` cadences are month-reset; the GEFS `5-day` /
# `10-day` forecast-horizon rows share the `10D` / `5D` stride but are
# daily-issued forecasts, so they keep the fixed `date_windows` walk.
_DEKAD_START_DAYS: tuple[int, ...] = (1, 11, 21)
_PENTAD_START_DAYS: tuple[int, ...] = (1, 6, 11, 16, 21, 26)
_PERIOD_START_DAYS: dict[str, tuple[int, ...]] = {
    "dekadal": _DEKAD_START_DAYS,
    "pentadal": _PENTAD_START_DAYS,
}


def _period_start_days(temporal_resolution: str | None) -> tuple[int, ...] | None:
    """Return the month-reset start days for a dekadal/pentadal cadence.

    Args:
        temporal_resolution: The dataset's `temporal_resolution` string.

    Returns:
        tuple[int, ...] | None: The `(1, 11, 21)` dekad or
        `(1, 6, 11, 16, 21, 26)` pentad start days, or `None` for any
        other cadence (which is walked by the shared fixed-stride
        `date_windows`).
    """
    if temporal_resolution is None:
        return None
    return _PERIOD_START_DAYS.get(temporal_resolution)


def _period_start_floor(
    date: pd.Timestamp, start_days: tuple[int, ...]
) -> pd.Timestamp:
    """Snap `date` back to the start of the dekad/pentad that contains it.

    Args:
        date: Any timestamp within a calendar period.
        start_days: The month-reset start days for the cadence.

    Returns:
        pd.Timestamp: The containing period's first day (same month).
    """
    return date.replace(day=max(d for d in start_days if d <= date.day))


def _next_period_start(date: pd.Timestamp, start_days: tuple[int, ...]) -> pd.Timestamp:
    """Return the start of the dekad/pentad after the one `date` begins.

    Args:
        date: A timestamp on or within a calendar period.
        start_days: The month-reset start days for the cadence.

    Returns:
        pd.Timestamp: The next period's first day -- the next start day
        in the same month, or the first start day of the next month when
        `date` is in the month's final (short) period.
    """
    later = [d for d in start_days if d > date.day]
    if later:
        return date.replace(day=later[0])
    return (date + pd.offsets.MonthBegin(1)).replace(day=start_days[0])


def _calendar_period_end(
    date: pd.Timestamp, start_days: tuple[int, ...]
) -> pd.Timestamp:
    """Return the inclusive last day of the dekad/pentad `date` begins.

    The day before the next period's start, which for the month's final
    period is month-end (the short 8-11 / 3-6 day remainder).

    Args:
        date: The period's start timestamp.
        start_days: The month-reset start days for the cadence.

    Returns:
        pd.Timestamp: The period's inclusive end day.
    """
    return _next_period_start(date, start_days) - pd.Timedelta(1, "D")


def _calendar_period_index(
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    start_days: tuple[int, ...],
) -> pd.DatetimeIndex:
    """Month-aligned dekad/pentad period starts over `[window_start, window_end]`.

    The first entry is the period *containing* `window_start` (snapped
    back to its calendar start), and each following entry steps one
    dekad/pentad on the month-reset grid, up to and including the period
    whose start is `<= window_end`. Unlike a fixed `10D` / `5D` stride,
    this never drifts off the month boundary, so every calendar period
    in range is visited exactly once.

    Args:
        window_start: First timestamp of the (clamped) request window.
        window_end: Last timestamp of the (clamped) request window.
        start_days: The month-reset start days for the cadence.

    Returns:
        pd.DatetimeIndex: The period-start timestamps to fetch.
    """
    starts: list[pd.Timestamp] = []
    current = _period_start_floor(window_start, start_days)
    while current <= window_end:
        starts.append(current)
        current = _next_period_start(current, start_days)
    return pd.DatetimeIndex(starts)


def _open_ftp() -> FTP:
    """Open an anonymous FTP session against CHC and return it (L5 helper).

    Module-level so the no-nested-function rule (`feedback_no_nested_functions`)
    is honoured. Callers are responsible for closing the returned session
    via `_close_ftp_quietly` (or replacing it via `_reopen_ftp`).

    Returns:
        FTP: A logged-in anonymous FTP client pointed at
        `CHIRPS.api_url` (`data.chc.ucsb.edu`).
    """
    ftp = FTP(CHIRPS.api_url)  # nosec B321 - public anonymous data FTP, no creds
    ftp.login()
    return ftp


def _close_ftp_quietly(ftp: FTP) -> None:
    """Close an FTP session, swallowing any errors (L5 helper).

    Used in `finally` blocks where the session may already be broken
    (e.g. after a partial download) -- raising during cleanup would
    mask the real failure.
    """
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001 - best-effort cleanup, never raises
        try:
            ftp.close()
        except Exception:  # noqa: BLE001  # nosec B110
            pass


def _reopen_ftp(ftp: FTP) -> FTP:
    """Close `ftp` and return a fresh anonymous session (L5 helper).

    Called from the sequential branch of `_download_dataset` after a
    per-date failure: the previous FTP socket may be in an unrecoverable
    state (broken pipe, half-read response) and reusing it would fail
    every subsequent date. Trading one extra anonymous login for
    correctness is the obvious win.
    """
    _close_ftp_quietly(ftp)
    return _open_ftp()


def _snap_bbox_outward(
    bbox: tuple[float, float, float, float],
    geo: tuple[float, ...] | list[float],
) -> list[float]:
    """Grow a bbox out to the enclosing cell edges of a north-up grid.

    `crop_to_aoi(..., touch=False)` keeps only the cells lying **fully
    inside** the box, so a request that cuts through a cell loses that whole
    cell. CHC grids are fine (CHIRPS is 0.05°) and a caller's bbox is almost
    never cell-aligned, so passing the raw request would shrink every output
    by up to one cell per edge and stop it covering the extent the API
    documents. Snapping the box out to the cell boundaries that enclose it
    first makes the inner crop select exactly the **outer** window — the same
    superset the hand-rolled `floor`/`ceil` slice used to produce — while the
    crop itself stays pyramids'.

    The snap is unclamped: a box reaching past the granule is left past it,
    and `crop` bounds the window to the raster. An already-aligned edge is
    left untouched (`floor`/`ceil` of a whole number is itself).

    Args:
        bbox: The requested `(west, south, east, north)` in the grid's CRS.
        geo: The granule's GDAL geotransform. Only a north-up, axis-aligned
            transform is supported — `geo[2]` / `geo[4]` (rotation) are
            assumed zero, which every CHC product satisfies.

    Returns:
        list[float]: `[west, south, east, north]`, each moved outward to the
            enclosing cell edge.

    Examples:
        - A misaligned box grows to the enclosing cells; an aligned one does not:
            ```python
            >>> geo = (-180.0, 1.0, 0.0, 50.0, 0.0, -1.0)
            >>> _snap_bbox_outward((0.5, 0.5, 9.5, 9.5), geo)
            [0.0, 0.0, 10.0, 10.0]
            >>> _snap_bbox_outward((0.0, 0.0, 10.0, 10.0), geo)
            [0.0, 0.0, 10.0, 10.0]

            ```
    """
    west, south, east, north = (float(v) for v in bbox)
    origin_x, pix_x, origin_y = float(geo[0]), float(geo[1]), float(geo[3])
    pix_y = -float(geo[5])
    snapped_west = origin_x + math.floor((west - origin_x) / pix_x) * pix_x
    snapped_east = origin_x + math.ceil((east - origin_x) / pix_x) * pix_x
    snapped_north = origin_y - math.floor((origin_y - north) / pix_y) * pix_y
    snapped_south = origin_y - math.ceil((origin_y - south) / pix_y) * pix_y
    return [snapped_west, snapped_south, snapped_east, snapped_north]


def _reject_unsigned_for_nodata_sentinel(dtype: np.dtype) -> None:
    """Bail out if the input raster's dtype can't carry a `-9999` no-data sentinel.

    The post-processing path normalises every negative pixel to -9999 and
    then casts back to the input dtype. For unsigned integer dtypes
    (`uint8`, `uint16`, `uint32`, `uint64`) the cast wraps -9999 into a
    positive value (`55537` for `uint16`, etc.), and the output band's
    declared `no_data_value=-9999` then matches *no* pixel. Refuse to
    proceed so the failure is loud, not silent.

    Every CHC raster shipped through the catalog today is float32; this
    guard is a defence in depth for future products. The per-date
    failure handling in `_download_dataset` catches the `TypeError` and
    skips the date.

    Args:
        dtype: The numpy dtype of the input raster array.

    Raises:
        TypeError: If `dtype` is any unsigned-integer numpy dtype.
    """
    if np.issubdtype(dtype, np.unsignedinteger):
        raise TypeError(
            f"CHC no-data normalisation cannot use a -9999 sentinel "
            f"on unsigned dtype {dtype}: the cast would wrap -9999 to "
            "a positive value and the declared no_data_value would "
            "match no pixel. Add an explicit no-data handling path "
            "for unsigned products before extending the catalog with "
            "one."
        )


def _log_date_failures(
    ds_key: str,
    var: Variable,
    failed: list[tuple[pd.Timestamp, BaseException]],
    *,
    total: int,
) -> None:
    """Warn once for a batch's per-date failures, naming the first three.

    Args:
        ds_key: The catalog dataset key.
        var: The variable being fetched.
        failed: The `(date, exception)` pairs that failed.
        total: How many dates were attempted, for the ratio.
    """
    sample = ", ".join(
        f"{date.date()} ({type(exc).__name__})" for date, exc in failed[:3]
    )
    tail = "" if len(failed) <= 3 else f" (+{len(failed) - 3} more)"
    logger.warning(
        f"{ds_key}/{var.name}: {len(failed)}/{total} dates "
        f"failed; first 3: {sample}{tail}"
    )


def _describe_pair(pair: tuple[str, str]) -> str:
    """Render a `(dataset, variable)` pair for the `_run_items` log lines.

    Args:
        pair: The `(dataset key, variable name)` that failed.

    Returns:
        str: `"<dataset>/<variable>"`.
    """
    return f"{pair[0]}/{pair[1]}"


class CHIRPS(AbstractDataSource):
    """CHIRPS catalog-driven FTP backend.

    Public surface: construct with a date range, a bbox, and
    `variables` (either a flat `list[str]` for the legacy
    CHIRPS-2.0 global path, or `dict[str, list[str]]` mapping a
    catalog dataset key to variable codes), then call
    `download`.

    Attributes:
        api_url: FTP hostname. Anonymous login; no credentials.
        catalog: `Catalog` instance loaded
            once at construction; resolves dataset keys to metadata.
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: CHIRPS honours `aggregate=` by reducing the per-date GeoTIFF stack it
    #: already writes into per-window rasters (daily → monthly, etc.) — a raster
    #: reduction through `earthlens.aggregate`'s shared `window_groups` /
    #: `reduce_time_axis` primitives, distinct from the NetCDF `aggregate_netcdf`
    #: node the ECMWF/S3 backends use. The centralised gate in
    #: `AbstractDataSource` then accepts it with no per-backend plumbing (see
    #: `download` → `_aggregate_pair`).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True
    api_url: str = "data.chc.ucsb.edu"

    @property
    def catalog(self):
        """The bundled CHC `Catalog` (alias of `_catalog`)."""
        return self._catalog

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: dict[str, list[str]] | list[str] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "daily",
        fmt: str = "%Y-%m-%d",
        path: Path | str | None = None,
    ):
        """Initialize a CHIRPS backend.

        Args:
            variables: Either a `list[str]` of variable codes for the
                legacy CHIRPS-2.0 global path (the dataset key is
                derived from `temporal_resolution`), or a
                `dict[str, list[str]]` mapping a catalog dataset key
                (e.g. `"africa-monthly"`) to a list of variable codes
                (e.g. `["precipitation"]`). Defaults to
                `["precipitation"]`.
            lat_lim: `[lat_min, lat_max]` in degrees. Defaults to
                `[-50, 50]` (the CHIRPS-2.0 global extent).
            lon_lim: `[lon_min, lon_max]` in degrees. Defaults to
                `[-180, 180]`.
            temporal_resolution: Only consulted when `variables` is a
                `list[str]`. Must be one of `"daily"` or `"monthly"`
                in that case (the only legacy values). Ignored when
                `variables` is already a dict. Defaults to `"daily"`.
            start: Inclusive start date as a string (parsed with
                `fmt`). `None` defaults to the earliest `start_date`
                across the requested datasets.
            end: Inclusive end date as a string. `None` defaults to
                today.
            path: Output directory. Created if it does not exist.
                Defaults to the configured earthlens output directory
                (`set_output_dir()` / `EARTHLENS_DATA_DIR`); see
                `earthlens.config`.
            fmt: `strptime` format for `start` / `end`. Defaults to
                `"%Y-%m-%d"`.

        Raises:
            KeyError: If a requested dataset key is not in the
                catalog, or a variable code is not declared under
                that dataset.
            ValueError: If `temporal_resolution` is outside
                `{"daily", "monthly"}` with a list-shape `variables`
                (N2 -- the list-shape API can only resolve those two
                legacy keys; switch to dict-shape `variables` for
                anything else).
        """
        if lat_lim is None:
            lat_lim = [-50.0, 50.0]
        if lon_lim is None:
            lon_lim = [-180.0, 180.0]

        catalog = Catalog()
        normalized = self._normalize_variables(variables, temporal_resolution)
        self._validate_keys(catalog, normalized)

        if start is None:
            start = min(catalog.datasets[k].start_date for k in normalized)
        if end is None:
            end = str(pd.Timestamp.now().date())

        self._catalog = catalog

        super().__init__(
            start=start,
            end=end,
            variables=normalized,
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    @staticmethod
    def _normalize_variables(
        variables: dict[str, list[str]] | list[str] | None,
        temporal_resolution: str,
    ) -> dict[str, list[str]]:
        """Coerce the user's `variables` to the catalog-keyed dict shape.

        Raises:
            ValueError: If a list-shape `variables` is paired with a
                `temporal_resolution` outside `{"daily", "monthly"}`.
                Pre-N2 this raised `KeyError`, which was inaccurate
                (the check is a value-membership check, not a dict
                lookup) -- the new `ValueError` matches the idiomatic
                Python distinction.
        """
        if variables is None:
            variables = ["precipitation"]
        if isinstance(variables, dict):
            return {k: list(v) for k, v in variables.items()}
        if temporal_resolution not in _LEGACY_DATASET_KEY:
            raise ValueError(
                f"temporal_resolution {temporal_resolution!r} is not "
                "supported by the list-shape `variables` API. Either "
                "pass a dict like `variables={'<dataset-key>': [...]}` "
                "or use one of "
                f"{sorted(_LEGACY_DATASET_KEY)}."
            )
        return {_LEGACY_DATASET_KEY[temporal_resolution]: list(variables)}

    @staticmethod
    def _validate_keys(catalog: Catalog, variables: dict[str, list[str]]) -> None:
        """Reject unknown dataset keys / variable names before download."""
        for ds_key, var_names in variables.items():
            if ds_key not in catalog.datasets:
                raise KeyError(
                    f"{ds_key!r} is not a curated CHIRPS dataset. "
                    "See `Catalog().list_datasets()` for available keys."
                )
            available = catalog.datasets[ds_key].variables
            for var_name in var_names:
                if var_name not in available:
                    raise KeyError(
                        f"variable {var_name!r} is not declared under "
                        f"{ds_key!r}. Available: {sorted(available)}."
                    )

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the user's `[start, end]` window.

        Per-dataset date ranges are derived in
        `_download_dataset` from each dataset's
        `pandas_freq`; this method only stores the outer window so
        consumers (and the abstract base's `self.time`) can see it.

        Args:
            start: Inclusive start date as a string.
            end: Inclusive end date as a string.
            temporal_resolution: Accepted for API symmetry; ignored
                here because the real frequency comes from the
                catalog per dataset.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen outer window. Only `start_date` /
            `end_date` carry meaning — `resolution` is a daily
            placeholder and `dates` is an empty
            `pandas.DatetimeIndex` because CHIRPS download
            cadence is per-dataset (`pandas_freq` lives on
            `Dataset`, not on the bbox-level
            outer window). A consumer iterating
            `self.time.dates` would otherwise get a misleading daily
            index for a `monthly` or `6-hourly` dataset.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution="D",
            dates=pd.DatetimeIndex([]),
        )

    def _create_grid(self, lat_lim: list[float], lon_lim: list[float]) -> SpatialExtent:
        """Return a `SpatialExtent` for the user's bbox.

        Returns:
            SpatialExtent: Frozen bbox with `resolution=0.05`,
            CHIRPS's primary native cell size. (Datasets at coarser
            pixels, such as WBGT 1° or africa-6-hourly 0.10°, are
            still clipped correctly because `_clip_to_bbox`
            reads the actual pixel size from the downloaded raster.)
        """
        return SpatialExtent.from_pairs(
            lat_lim=lat_lim, lon_lim=lon_lim, resolution=0.05
        )

    def download(
        self,
        progress_bar: bool = True,
        cores: int | None = None,
        errors: str = "warn",
        aggregate: AggregationConfig | None = None,
        **_kwargs: object,
    ) -> list[Path]:
        """Download every `(dataset, variable)` pair in `self.vars`.

        Args:
            progress_bar: Whether to show a per-dataset tqdm progress
                bar. Defaults to `True`.
            cores: Number of joblib workers for parallel per-date
                retrieval. `None` (or `0`) runs sequentially.
            errors: Partial-failure policy across the
                `(dataset, variable)` pairs — `"warn"` (default) logs each
                failure and continues, `"raise"` propagates the first one,
                `"ignore"` continues silently.
            aggregate: Optional `earthlens.aggregate.AggregationConfig`. When
                set, the per-date GeoTIFFs fetched for each `(dataset, variable)`
                are grouped by `aggregate.freq` and each window reduced to one
                raster through the shared `window_groups` / `reduce_time_axis`
                primitives — a raster reduction (daily → monthly, etc.), distinct
                from the NetCDF `aggregate_netcdf` path S3/ECMWF use. `op="auto"`
                resolves per variable (flux variables such as precipitation sum,
                state variables average). Results land under
                `aggregate.out_dir`, defaulting to `<self.root_dir>/aggregated/`,
                and the returned list is the per-window rasters rather than the
                per-date ones. Discrete multi-year archive datasets (CenTrends)
                have no per-date stack to reduce and reject it.
            **_kwargs: Reserved for other forwarded keyword arguments.

        Returns:
            list[Path]: The written GeoTIFF paths
            (`<self.root_dir>/<ds_key>_<var_name>_<date>.tif`), across
            every `(dataset, variable)` and date — or, when `aggregate` is set,
            the per-window reduced rasters at
            `<aggregate.out_dir or self.root_dir/aggregated>/<ds_key>_<var>_<op>_<freq>_<window>.tif`.
            Under the default `errors="warn"`, per-variable and per-date failures
            are logged and omitted from the list rather than aborting the batch.

        Raises:
            ValueError: If `errors` is not a recognised policy, or if `aggregate`
                is set for a discrete multi-year archive dataset.

        Examples:
            - Legacy shape (CHIRPS-2.0 global daily):

                ```python
                >>> from earthlens.chc import CHIRPS  # doctest: +SKIP
                >>> CHIRPS(  # doctest: +SKIP
                ...     variables=["precipitation"],
                ...     temporal_resolution="daily",
                ...     start="2009-01-01", end="2009-01-02",
                ...     lat_lim=[4.0, 5.0], lon_lim=[-75.0, -74.0],
                ...     path="out/",
                ... ).download()

                ```
            - Catalog shape (pulls Africa pentadal precipitation):

                ```python
                >>> from earthlens.chc import CHIRPS  # doctest: +SKIP
                >>> CHIRPS(  # doctest: +SKIP
                ...     variables={"africa-pentad": ["precipitation"]},
                ...     start="2020-01-01", end="2020-02-01",
                ...     lat_lim=[-5.0, 5.0], lon_lim=[30.0, 40.0],
                ...     path="out/",
                ... ).download()

                ```
        """
        assert isinstance(self.vars, dict)  # CHC normalises variables to a mapping
        effective_aggregate = self._effective_aggregate(aggregate)
        pairs = [
            (ds_key, var_name)
            for ds_key, var_names in self.vars.items()
            for var_name in var_names
        ]
        per_pair_paths, failures = self._run_items(
            pairs,
            partial(
                self._download_pair,
                progress_bar=progress_bar,
                cores=cores,
                aggregate=effective_aggregate,
            ),
            errors=errors,
            label="variable",
            describe=_describe_pair,
        )
        if not failures:
            logger.info(
                f"CHIRPS download summary: all {len(pairs)} variables succeeded."
            )
        return [path for paths in per_pair_paths for path in paths]

    def _download_pair(
        self,
        pair: tuple[str, str],
        *,
        progress_bar: bool,
        cores: int | None,
        aggregate: AggregationConfig | None = None,
    ) -> list[Path]:
        """Download one `(dataset, variable)` pair across the whole date range.

        Args:
            pair: The `(dataset key, variable name)` to fetch.
            progress_bar: Whether to show the per-dataset tqdm bar.
            cores: joblib worker count for the per-date retrieval.
            aggregate: Optional aggregation spec. When set, the per-date rasters
                are reduced to per-window ones via `_aggregate_pair` and those
                are returned in place of the per-date outputs.

        Returns:
            list[Path]: The GeoTIFFs written for that pair — per-date, or the
                per-window reductions when `aggregate` is set.
        """
        ds_key, var_name = pair
        dataset = self._catalog.datasets[ds_key]
        var = dataset.variables[var_name]
        logger.info(
            f"Download CHIRPS {ds_key}/{var_name} from "
            f"{self.time.start_date.date()} to {self.time.end_date.date()}"
        )
        paths = self._download_dataset(
            ds_key,
            dataset,
            var,
            progress_bar=progress_bar,
            cores=cores,
        )
        if aggregate is None:
            return paths
        return self._aggregate_pair(ds_key, dataset, var, paths, aggregate)

    def _effective_aggregate(
        self, aggregate: AggregationConfig | None
    ) -> AggregationConfig | None:
        """Default an aggregate spec's `out_dir` to `<root_dir>/aggregated/`.

        Mirrors the ECMWF / S3 convention: an explicit `out_dir` wins, and an
        unset one lands the per-window rasters under `aggregated/` beside the
        per-date outputs rather than mixing them into the same directory.

        Args:
            aggregate: The caller's spec, or `None` when no aggregation was
                requested.

        Returns:
            AggregationConfig | None: `None` unchanged, otherwise a copy whose
                `out_dir` is resolved.
        """
        resolved = aggregate
        if aggregate is not None and aggregate.out_dir is None:
            resolved = aggregate.model_copy(
                update={"out_dir": self.root_dir / "aggregated"}
            )
        return resolved

    @staticmethod
    def _resolve_aggregate_op(op: str, var: Variable) -> str:
        """Turn `op="auto"` into a concrete reducer from the variable's kind.

        A flux quantity (precipitation) accumulates, so a daily → monthly
        reduction sums; a state quantity (temperature) averages. Any explicit
        op is returned unchanged. This mirrors
        `earthlens.aggregate._resolve_op` without importing a
        distribution-private core symbol across the provider boundary.

        Args:
            op: The `AggregationConfig.op` value, possibly `"auto"`.
            var: The catalog row for the variable being reduced.

        Returns:
            str: The concrete reducer name for `reduce_time_axis`.
        """
        if op == "auto":
            result = "sum" if var.is_flux else "mean"
        else:
            result = op
        return result

    def _aggregate_pair(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        paths: list[Path],
        config: AggregationConfig,
    ) -> list[Path]:
        """Reduce one pair's per-date rasters into per-window rasters.

        Pairs each written GeoTIFF back to its date — the per-date names are
        deterministic, so a `{filename: date}` map rebuilt from
        `_overlapping_dates` recovers the alignment without threading dates
        through the fetchers — buckets the dates by `config.freq` via
        `window_groups`, and reduces each window one at a time: only the
        rasters in the current window are opened, read with nodata as NaN,
        stacked and reduced with `reduce_time_axis`, then closed before the
        next window. One GeoTIFF per window lands under `config.out_dir`.

        Reducing per window — rather than stacking the whole matched range at
        once — keeps the memory footprint bounded by the largest single window,
        so a long daily request does not materialise every per-date array
        simultaneously.

        For a flux `op` (`"sum"`, the `auto` default for precipitation) an
        all-nodata window reads as `0.0` rather than nodata, because the
        NaN-aware sum of an all-NaN slice is zero; set `config.min_count` to
        require a minimum count of valid dates per window when that distinction
        matters.

        Args:
            ds_key: The catalog dataset key.
            dataset: The resolved catalog row.
            var: The variable being reduced.
            paths: The per-date GeoTIFFs written for this pair (a subset of the
                requested dates under `errors="warn"`).
            config: The aggregation spec (`freq`, `op`, `skipna`, `min_count`,
                `out_dir`).

        Returns:
            list[Path]: The per-window reduced GeoTIFFs, empty when nothing was
                written for this pair.

        Raises:
            ValueError: If `dataset` is a discrete archive (no per-date stack to
                reduce), or the per-date grids differ in shape and cannot be
                stacked.
        """
        from earthlens.aggregate import window_groups

        if dataset.is_discrete:
            raise ValueError(
                f"cannot aggregate {ds_key!r}: it publishes fixed multi-year "
                "archive files, not a per-date stack to reduce over time. Drop "
                "aggregate= for this dataset."
            )

        dated = self._dated_outputs(ds_key, dataset, var, paths)
        op = self._resolve_aggregate_op(config.op, var)
        out_dir = Path(config.out_dir) if config.out_dir is not None else self.root_dir
        out: list[Path] = []
        if not dated:
            return out

        geo, epsg = self._validate_grid(ds_key, var, dated)
        out_dir.mkdir(parents=True, exist_ok=True)
        time_axis = pd.DatetimeIndex([date for date, _ in dated])
        for label, mask in window_groups(time_axis, config.freq):
            window_paths = [path for keep, (_, path) in zip(mask, dated) if keep]
            reduced = self._reduce_window(window_paths, op, config)
            written = Dataset.from_array(
                reduced,
                no_data_value=-9999.0,
                geo_ref=GeoReference(geo=geo, epsg=epsg),
            )
            target = out_dir / self._window_filename(ds_key, var, op, config, label)
            written.to_file(str(target))
            close_quietly(written)
            out.append(target)
        return out

    def _dated_outputs(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        paths: list[Path],
    ) -> list[tuple[pd.Timestamp, Path]]:
        """Pair each written per-date GeoTIFF with its date, sorted by date.

        Args:
            ds_key: The catalog dataset key.
            dataset: The resolved catalog row.
            var: The variable the per-date rasters belong to.
            paths: The GeoTIFFs written for this pair.

        Returns:
            list[tuple[pandas.Timestamp, Path]]: `(date, path)` for every path
                whose canonical name maps back to an overlapping date, sorted
                ascending by date.
        """
        dates = self._overlapping_dates(ds_key, dataset)
        name_to_date = {
            self._output_filename(ds_key, dataset, var, date): date
            for date in (dates if dates is not None else [])
        }
        return sorted(
            (
                (name_to_date[path.name], path)
                for path in paths
                if path.name in name_to_date
            ),
            key=lambda item: item[0],
        )

    def _validate_grid(
        self,
        ds_key: str,
        var: Variable,
        dated: list[tuple[pd.Timestamp, Path]],
    ) -> tuple[tuple[float, float, float, float, float, float] | None, int | None]:
        """Confirm the per-date grids line up and capture the shared georef.

        Opens each raster for its metadata only (no array read), so the check
        is cheap even for a long date range, and returns the first raster's
        geotransform and EPSG for the reduced windows to inherit.

        Args:
            ds_key: The catalog dataset key, named in the mismatch error.
            var: The variable being reduced, named in the mismatch error.
            dated: The non-empty `(date, path)` pairs from `_dated_outputs`.

        Returns:
            tuple: The shared `(geotransform, epsg)` for every per-date raster.

        Raises:
            ValueError: If the per-date grids differ in shape and cannot be
                stacked.
        """
        shapes: set[tuple[int, int]] = set()
        geo: tuple[float, float, float, float, float, float] | None = None
        epsg: int | None = None
        for _, path in dated:
            ds = Dataset.read_file(str(path))
            try:
                shapes.add((ds.rows, ds.columns))
                if geo is None:
                    geo = ds.geotransform
                    epsg = ds.epsg
            finally:
                close_quietly(ds)
        if len(shapes) > 1:
            raise ValueError(
                f"cannot aggregate {ds_key}/{var.name}: its per-date grids "
                f"differ in shape and cannot be stacked — {sorted(shapes)}. "
                "Re-run the download, or request one consistent AOI."
            )
        return geo, epsg

    def _reduce_window(
        self,
        window_paths: list[Path],
        op: str,
        config: AggregationConfig,
    ) -> np.ndarray:
        """Open one window's rasters, reduce them, and fill nodata with -9999.

        Only this window's rasters are opened and closed here, so peak memory
        tracks the largest window rather than the whole date range.

        Args:
            window_paths: The per-date GeoTIFFs falling in this window.
            op: The concrete reducer (`"sum"`, `"mean"`, …) from
                `_resolve_aggregate_op`.
            config: The aggregation spec supplying `skipna` and `min_count`.

        Returns:
            numpy.ndarray: The reduced 2-D band, nodata pixels set to -9999.0.
        """
        from earthlens.aggregate import reduce_time_axis

        datasets = [Dataset.read_file(str(path)) for path in window_paths]
        try:
            stack = np.stack([self._band_as_nan(ds) for ds in datasets])
        finally:
            for ds in datasets:
                close_quietly(ds)
        reduced = reduce_time_axis(
            stack,
            op,
            skipna=config.skipna,
            min_count=config.min_count,
        )
        filled: np.ndarray = np.where(np.isnan(reduced), -9999.0, reduced)
        return filled

    @staticmethod
    def _window_filename(
        ds_key: str,
        var: Variable,
        op: str,
        config: AggregationConfig,
        label: pd.Timestamp,
    ) -> str:
        """Build the per-window output filename.

        The window label is stamped as `%Y%m%d`; a sub-daily `config.freq`
        (e.g. `"6h"`) also stamps `%H%M` so two windows inside one day do not
        collapse to the same name and overwrite each other.

        Args:
            ds_key: The catalog dataset key.
            var: The variable being reduced.
            op: The concrete reducer used.
            config: The aggregation spec (supplies `freq`).
            label: The window's start timestamp.

        Returns:
            str: The `<ds_key>_<var>_<op>_<freq>_<stamp>.tif` filename.
        """
        nanos_per_day = 86_400_000_000_000
        try:
            is_subdaily = (
                pd.tseries.frequencies.to_offset(config.freq).nanos < nanos_per_day
            )
        except (ValueError, TypeError):
            is_subdaily = False
        stamp = label.strftime("%Y%m%d%H%M" if is_subdaily else "%Y%m%d")
        return f"{ds_key}_{var.name}_{op}_{config.freq}_{stamp}.tif"

    @staticmethod
    def _band_as_nan(ds: Dataset) -> np.ndarray:
        """Return a raster's first band as float64 with nodata as NaN.

        Reads through pyramids' masked read so the band's own declared no-data
        (CHIRPS normalises missing to -9999) becomes NaN, which the NaN-aware
        reducers then skip. A 3-D single-band array is collapsed to its first
        band.

        Args:
            ds: An open `pyramids.dataset.Dataset` for one per-date raster.

        Returns:
            numpy.ndarray: The 2-D band as float64, nodata filled with NaN.
        """
        array: np.ndarray = np.ma.filled(
            ds.read_array(masked=True).astype("float64"), np.nan
        )
        return array[0] if array.ndim == 3 else array

    def _download_dataset(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        progress_bar: bool = True,
        cores: int | None = None,
    ) -> list[Path]:
        """Iterate the per-dataset date range and dispatch `_api`.

        Branches on `dataset.is_discrete`: datasets that publish a fixed
        set of multi-year archive files (`discrete_files`, e.g. CenTrends)
        are routed through `_download_discrete`, which fetches each
        listed filename once instead of doing date substitution.

        Returns:
            list[Path]: The GeoTIFF paths written for this `(ds, var)`
                (empty when the window does not overlap the dataset, or
                every date was skipped / failed).
        """
        if dataset.is_discrete:
            return self._download_discrete(
                ds_key, dataset, var, progress_bar=progress_bar
            )

        dates = self._overlapping_dates(ds_key, dataset)
        if dates is None:
            return []
        # M1: catch per-date failures so a single transient (TCP reset,
        # FTP 550, a one-off bad raster) doesn't abort the rest of the
        # batch for this `(ds, var)`. The outer `download()` loop kept
        # its (ds, var)-level policy as defence in depth for
        # catalog-resolution / never-reach-the-network failures.
        if cores:
            paths, failed = self._fetch_dates_parallel(
                ds_key, dataset, var, dates, cores
            )
        else:
            paths, failed = self._fetch_dates_sequential(
                ds_key, dataset, var, dates, progress_bar=progress_bar
            )
        if failed:
            _log_date_failures(ds_key, var, failed, total=len(dates))
        return paths

    def _overlapping_dates(
        self, ds_key: str, dataset: ChcDataset
    ) -> pd.DatetimeIndex | None:
        """Clamp the request window to the dataset's own and expand it to dates.

        Args:
            ds_key: The catalog dataset key, named in the no-overlap warning.
            dataset: The resolved catalog row (carries its publication window
                and cadence).

        Returns:
            pandas.DatetimeIndex | None: The dates to fetch, or `None` when the
                request window does not overlap the dataset at all — which is a
                warning and an empty result, not an error.

        Dekadal and pentadal datasets are walked on the month-reset
        calendar grid (`_calendar_period_index`) rather than a fixed
        `10D` / `5D` `pandas_freq` stride, so every calendar period is
        visited exactly once; all other cadences use the shared
        fixed-stride `date_windows`.
        """
        ds_start = pd.Timestamp(dataset.start_date)
        ds_end = pd.Timestamp(dataset.end_date) if dataset.end_date else None
        window_start = max(self.time.start_date, ds_start)
        window_end = (
            self.time.end_date if ds_end is None else min(self.time.end_date, ds_end)
        )
        if window_start > window_end:
            logger.warning(
                f"{ds_key}: requested window "
                f"[{self.time.start_date.date()}, "
                f"{self.time.end_date.date()}] does not overlap dataset "
                f"window [{ds_start.date()}, "
                f"{ds_end.date() if ds_end else 'now'}]; skipping"
            )
            return None
        start_days = _period_start_days(dataset.temporal_resolution)
        if start_days is not None:
            return _calendar_period_index(window_start, window_end, start_days)
        return date_windows(window_start, window_end, dataset.pandas_freq)

    def _fetch_dates_sequential(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        dates: pd.DatetimeIndex,
        *,
        progress_bar: bool,
    ) -> tuple[list[Path], list[tuple[pd.Timestamp, BaseException]]]:
        """Fetch every date over one shared FTP session.

        L5: one anonymous login serves the whole batch instead of one per date.
        A per-date failure closes the session and opens a fresh one before the
        next iteration, so a broken socket from one bad date cannot poison the
        rest — the recovery step that keeps this loop out of `_run_items`.

        Args:
            ds_key: The catalog dataset key.
            dataset: The resolved catalog row.
            var: The variable being fetched.
            dates: The dates to fetch.
            progress_bar: Whether to show the per-dataset tqdm bar.

        Returns:
            Tuple of the written paths and the `(date, exception)` failures.
        """
        paths: list[Path] = []
        failed: list[tuple[pd.Timestamp, BaseException]] = []
        ftp_session = _open_ftp()
        try:
            for date in tqdm(dates, desc=f"CHIRPS {ds_key}", disable=not progress_bar):
                try:
                    path = self._api(ds_key, dataset, var, date, ftp=ftp_session)
                except Exception as exc:  # noqa: BLE001 - log + continue per date
                    failed.append((date, exc))
                    ftp_session = _reopen_ftp(ftp_session)
                else:
                    if path is not None:
                        paths.append(path)
        finally:
            _close_ftp_quietly(ftp_session)
        return paths, failed

    def _fetch_dates_parallel(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        dates: pd.DatetimeIndex,
        cores: int,
    ) -> tuple[list[Path], list[tuple[pd.Timestamp, BaseException]]]:
        """Fetch the dates across joblib workers, each with its own login.

        A worker cannot share the unpicklable FTP socket, so this branch pays a
        login per file — the trade for the parallelism.

        Args:
            ds_key: The catalog dataset key.
            dataset: The resolved catalog row.
            var: The variable being fetched.
            dates: The dates to fetch.
            cores: Number of joblib workers.

        Returns:
            Tuple of the written paths and the `(date, exception)` failures.
        """
        results = Parallel(n_jobs=cores)(
            delayed(self._api_or_capture)(ds_key, dataset, var, date) for date in dates
        )
        paths = [path for path, _exc in results if path is not None]
        failed = [exc for _path, exc in results if exc is not None]
        return paths, failed

    def _api_or_capture(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        date: pd.Timestamp,
    ) -> tuple[Path | None, tuple[pd.Timestamp, BaseException] | None]:
        """Run `_api`, capturing the path or the exception (M1 helper).

        Used by the joblib-parallel branch of `_download_dataset` so a
        single bad date doesn't take down the rest of the batch.
        Returns `(path, None)` on success (`path` is `None` when the
        date was skipped) and `(None, (date, exc))` on failure. The
        sequential branch handles its own try/except inline and does
        not call this.
        """
        try:
            path = self._api(ds_key, dataset, var, date)
            return (path, None)
        except Exception as exc:  # noqa: BLE001 - log + continue per date
            return (None, (date, exc))

    def _download_discrete(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        progress_bar: bool = True,
    ) -> list[Path]:
        """Fetch each entry in `dataset.discrete_files` once.

        For datasets that publish a fixed set of archive files
        (CenTrends, CHPclim v2, similar), date iteration is meaningless
        — each file is the whole product, not a per-date partition.
        Files are saved as `<ds_key>_<source_filename>` in
        `self.root_dir`.

        Post-processing branches on the catalog's `default_format`:

        * 2-D raster formats (`tif`, `cog`, `bil`) are clipped to the
          user's bbox in place via `_clip_raster_in_place`, just
          like the per-date `_post_process` path. CHPclim v2 monthly
          climatology TIFs flow through here.
        * Multi-dim / opaque formats (`netcdf`, `bin`) are passed
          through unmodified — CenTrends multi-year monthly NetCDFs
          carry a `time` axis that the 2-D clip math cannot handle
          correctly, so time-and-region subsetting is left to the
          caller (read with xarray and use
          `.sel(time=..., lat=..., lon=...)`).
        """
        fmt_key = dataset.default_format
        ftp_base = dataset.ftp_bases[fmt_key]
        assert dataset.discrete_files is not None  # discrete-file datasets set this
        filenames = dataset.discrete_files[fmt_key]
        is_2d_raster = fmt_key in {"tif", "cog", "bil"}
        iterable = tqdm(filenames, desc=f"CHC {ds_key}", disable=not progress_bar)
        # L5: one shared anonymous-login round-trip across the whole
        # discrete-files batch. CHPclim v2 in particular is 12 files
        # served from the same dir; pre-L5 that meant 12 logins.
        paths: list[Path] = []
        ftp_session = _open_ftp()
        try:
            for filename in iterable:
                local_path = self.root_dir / f"{ds_key}_{filename}"
                self._fetch_ftp(ftp_base, filename, local_path, ftp=ftp_session)
                if is_2d_raster:
                    self._clip_raster_in_place(local_path, signed=var.signed)
                paths.append(local_path)
        finally:
            _close_ftp_quietly(ftp_session)
        return paths

    def _clip_raster_in_place(self, path: Path, *, signed: bool = False) -> None:
        """Read a 2-D raster at `path`, clip to `self.space`, write back.

        Used by `_download_discrete` for `tif` / `cog` / `bil`
        outputs. For an unsigned variable negative pixels are normalised to
        -9999 (CHC's documented no-data sentinel); for a `signed` variable the
        genuine negatives are preserved. -9999 is declared as the output
        band's no-data value either way, mirroring the per-date
        `_post_process` behaviour.

        Args:
            path: The on-disk raster to clip and rewrite in place.
            signed: Forwarded to `_clip_and_normalise`; true preserves
                negative pixels (a signed variable such as an anomaly).
        """
        raster = Dataset.read_file(str(path))
        self._clip_and_normalise(raster, signed=signed).to_file(str(path))

    def _api(
        self,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        date: pd.Timestamp,
        ftp: FTP | None = None,
    ) -> Path | None:
        """Resolve the FTP URL for one date, fetch, and post-process.

        Args:
            ds_key: Catalog dataset key (e.g. `"global-daily"`).
            dataset: The `Dataset` row for `ds_key`.
            var: The `Variable` being downloaded.
            date: The per-date pandas Timestamp for the request.
            ftp: Optional shared FTP session (L5). When provided, the
                caller (a sequential `_download_dataset` batch) owns
                the connection lifecycle and `_fetch_ftp` reuses it
                instead of opening a per-file login. When `None`,
                `_fetch_ftp` opens and closes its own.

        Returns:
            Path: Output GeoTIFF on success.
            None: When the dataset's file pattern uses a placeholder
                this backend does not yet expand (tracked as M5 in the
                planning doc). A warning is logged and the date is
                skipped.
        """
        fmt_key = dataset.default_format
        ftp_base = dataset.ftp_bases[fmt_key]
        assert dataset.file_patterns is not None  # pattern datasets set this
        pattern = dataset.file_patterns[fmt_key]

        try:
            relative = pattern.format(
                **self._placeholders(
                    date,
                    pandas_freq=dataset.pandas_freq,
                    temporal_resolution=dataset.temporal_resolution,
                )
            )
        except KeyError as missing:
            logger.warning(
                f"{ds_key}: file pattern {pattern!r} requires "
                f"placeholder {missing} which is not yet expanded by "
                "the backend (see planning issue M5); skipping "
                f"{date.date()}"
            )
            return None

        if "/" in relative:
            subdir, _, remote_filename = relative.rpartition("/")
            remote_dir = f"{ftp_base.rstrip('/')}/{subdir}/"
        else:
            remote_dir = ftp_base
            remote_filename = relative

        local_compressed = self.root_dir / remote_filename
        try:
            self._fetch_ftp(remote_dir, remote_filename, local_compressed, ftp=ftp)
        except Exception:  # noqa: BLE001 - clean up the partial download on any FTP-stack failure, then re-raise unchanged
            if local_compressed.exists():
                try:
                    local_compressed.unlink()
                except OSError:
                    pass
            raise

        return self._post_process(local_compressed, ds_key, dataset, var, date)

    @staticmethod
    def _placeholders(
        date: pd.Timestamp,
        pandas_freq: str | None = None,
        temporal_resolution: str | None = None,
    ) -> dict[str, str]:
        """Build the format-string substitution dict for one date.

        Covers `{year}`, `{month}`, `{day}`, `{dekad}`, `{pentad}`,
        `{hour}`, `{doy}` (always) plus `{start_yyyymmdd}` /
        `{end_yyyymmdd}` (when `pandas_freq` is supplied, or
        `temporal_resolution` is `dekadal` / `pentadal`) -- the
        placeholders used by the curated datasets.

        The `{start_yyyymmdd}` / `{end_yyyymmdd}` pair (M5) is needed
        for WBGT, whose filenames carry the `[start, end]` endpoints
        of the period each timestep represents
        (e.g. `data_20200101_20200131.tif` for January 2020 monthly,
        `data_20200121_20200131.tif` for the third dekad of 2020).
        How the inclusive end is derived depends on the cadence:

        - Dekadal / pentadal (`temporal_resolution`): the period ends
          the day before the next calendar dekad/pentad start, which
          for the month's final period is month-end -- so the third
          dekad of January is `20200121 / 20200131`, not the `20200130`
          a fixed `10D` offset would give, and February's third dekad
          correctly ends on the 28th/29th.
        - Any other cadence (`pandas_freq`): `end = (date +
          offset(pandas_freq)) - 1 day`. For `MS` on `2020-01-01` this
          gives `20200101 / 20200131`; for daily (`D`) `end == date`.

        Other placeholders surface as a `KeyError` that `_api` catches
        and logs (`{month_pair}` for CHIRPS v3 2-monthly, `{res}` /
        `{scale}` -- none of those are wired today).

        Args:
            date: The pandas Timestamp for the per-date request.
            pandas_freq: Optional pandas offset alias from
                `Dataset.pandas_freq`, used to derive the period end for
                non-dekadal/pentadal cadences.
            temporal_resolution: Optional `Dataset.temporal_resolution`.
                When it is `"dekadal"` / `"pentadal"` the period end is
                computed on the month-reset calendar grid instead of
                from `pandas_freq`. When neither this nor `pandas_freq`
                is supplied, the `start_yyyymmdd` / `end_yyyymmdd` keys
                are omitted (the caller's pattern is assumed not to
                reference them).

        Returns:
            dict[str, str]: Placeholder names to substituted values.
        """
        day = date.day
        out = {
            "year": f"{date.year}",
            "month": f"{date.month:02d}",
            "day": f"{day:02d}",
            "dekad": str(min(3, ((day - 1) // 10) + 1)),
            "pentad": str(min(6, ((day - 1) // 5) + 1)),
            "hour": f"{date.hour:02d}",
            "doy": f"{date.dayofyear:03d}",
        }
        start_days = _period_start_days(temporal_resolution)
        if start_days is not None:
            period_end = _calendar_period_end(date, start_days)
            out["start_yyyymmdd"] = date.strftime("%Y%m%d")
            out["end_yyyymmdd"] = period_end.strftime("%Y%m%d")
        elif pandas_freq is not None:
            offset = pd.tseries.frequencies.to_offset(pandas_freq)
            # `date + offset` lands at the start of the NEXT period;
            # subtract one day to get the inclusive end of THIS period.
            # For daily (`D`) this gives `end == date`, which matches
            # CHC's daily-WBGT-style naming if any catalog row uses it.
            period_end = (date + offset) - pd.Timedelta(1, "D")
            out["start_yyyymmdd"] = date.strftime("%Y%m%d")
            out["end_yyyymmdd"] = period_end.strftime("%Y%m%d")
        return out

    @staticmethod
    def _fetch_ftp(
        remote_dir: str,
        remote_filename: str,
        local_path: Path,
        ftp: FTP | None = None,
    ) -> None:
        """Download one file via anonymous FTP into `local_path`.

        Args:
            remote_dir: Remote directory the file lives under (absolute
                path on `data.chc.ucsb.edu`). Always interpreted as an
                absolute path regardless of leading `/` -- the helper
                normalises to `"/<dir>"` before calling `ftp.cwd`. This
                matters for the shared-session path (L5): the session
                retains its working directory across calls, so a
                relative `cwd("products/...")` after a successful fetch
                into `products/.../2009/` would resolve to
                `products/.../2009/products/...` and 550. Forcing absolute paths
                makes every call independent of the previous one.
            remote_filename: File name within `remote_dir`.
            local_path: Local path to write the downloaded bytes to.
            ftp: Optional shared FTP session (L5). When provided, this
                method reuses the caller's already-logged-in session
                (one login per batch instead of one login per file).
                When `None`, opens a fresh anonymous login, runs the
                fetch, and closes the session before returning.
        """
        absolute_dir = "/" + remote_dir.lstrip("/")
        if ftp is not None:
            ftp.cwd(absolute_dir)
            with open(local_path, "wb") as fp:
                ftp.retrbinary(f"RETR {remote_filename}", fp.write)
            return
        with closing(_open_ftp()) as session:
            session.cwd(absolute_dir)
            with open(local_path, "wb") as fp:
                session.retrbinary(f"RETR {remote_filename}", fp.write)

    def _post_process(
        self,
        compressed_path: Path,
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        date: pd.Timestamp,
    ) -> Path:
        """Decompress (if `.gz`), clip to the user bbox, write a GeoTIFF."""
        local_path = compressed_path
        if str(compressed_path).endswith(".gz"):
            extracted = compressed_path.with_suffix("")
            extract_from_gz(str(compressed_path), str(extracted), delete=True)
            local_path = extracted

        raster = Dataset.read_file(str(local_path))
        out_path = self.root_dir / self._output_filename(ds_key, dataset, var, date)
        self._clip_and_normalise(raster, signed=var.signed).to_file(str(out_path))

        try:
            local_path.unlink(missing_ok=True)
        except (PermissionError, OSError):
            logger.warning(
                f"could not delete intermediate {local_path}; safe to "
                "remove after the download finishes"
            )
        return out_path

    def _clip_and_normalise(self, raster: Dataset, *, signed: bool = False) -> Dataset:
        """Normalise CHC negatives to the -9999 no-data sentinel and clip to the AOI.

        CHIRPS encodes "missing" as a negative value, and some rasters declare
        no no-data value at all. For an unsigned variable every negative pixel
        becomes -9999 and the output band declares -9999 as its no-data value;
        the raster is then clipped to the request bbox — or masked to the exact
        polygon when the `aoi=` carried one — through the shared `crop_to_aoi`,
        rather than a hand-rolled NumPy window slice. Normalising the whole
        granule before the crop lets the polygon mask flag out-of-shape cells
        with the now-declared -9999 no-data.

        When `signed` is true the negative-to-sentinel rewrite is skipped: a
        signed variable such as `precipitation_anomaly` carries genuine
        below-climatology negatives that must survive, while the raster's own
        -9999 fill is still honoured as no-data (it is declared on the output
        band either way). Without this, every below-normal pixel of an anomaly
        product would be silently nulled.

        The bbox is snapped **outward** to the granule's cell edges first (see
        `_snap_bbox_outward`), because `crop_to_aoi(touch=False)` keeps
        only cells lying fully inside the box. Passing the raw request would
        drop every partially-covered edge cell, so a CHIRPS output would span
        *less* than the extent the caller asked for. Snapping first makes the
        crop reproduce the outer, superset window the hand-rolled slice
        produced, while the crop itself stays pyramids'.

        Args:
            raster: The freshly read source `Dataset` (the whole granule).
            signed: When true, preserve negative pixels (a signed variable
                such as an anomaly); when false (the default), rewrite every
                negative to the -9999 no-data sentinel (precipitation fill).

        Returns:
            A new `Dataset` clipped (or polygon-masked) to the AOI, carrying
            -9999 as its no-data value. The bbox path is a superset of the
            request: partially-covered edge cells are kept.
        """
        data = raster.read_array()
        _reject_unsigned_for_nodata_sentinel(data.dtype)
        self._check_bbox_overlaps(data, raster.geotransform)
        nodata_sentinel: float = -9999.0
        request_bbox = [
            self.space.west,
            self.space.south,
            self.space.east,
            self.space.north,
        ]
        if getattr(self.space, "geometry", None) is not None:
            # Polygon path only: the mask flags out-of-shape cells with the
            # band's declared no-data, so -9999 has to be in place *before*
            # the crop. That costs a whole-granule normalise + rebuild.
            # `crop_to_aoi` masks to the geometry and ignores `bbox=` here, so
            # the request bbox is passed unsnapped (the snap is bbox-path-only).
            if not signed:
                data = np.where(data < 0, nodata_sentinel, data).astype(
                    data.dtype, copy=False
                )
            full = Dataset.from_array(
                data,
                no_data_value=nodata_sentinel,
                geo_ref=GeoReference(geo=raster.geotransform, epsg=raster.epsg),
            )
            return crop_to_aoi(full, self.space, bbox=request_bbox, touch=False)
        # Bbox path: crop first, normalise the window. A global CHIRPS daily
        # granule is ~7200x2000 and the request is usually a few hundred cells,
        # so normalising before the crop would allocate the whole granule twice
        # (the np.where copy and the rebuilt Dataset) per date, for nothing —
        # no mask is involved, so nothing depends on the sentinel being
        # declared up front. Snap the bbox out to the granule's cell edges so
        # `touch=False` keeps every partially-covered edge cell (see H1).
        bbox = _snap_bbox_outward(
            (request_bbox[0], request_bbox[1], request_bbox[2], request_bbox[3]),
            raster.geotransform,
        )
        cropped = crop_to_aoi(raster, self.space, bbox=bbox, touch=False)
        window = cropped.read_array()
        if not signed:
            window = np.where(window < 0, nodata_sentinel, window).astype(
                window.dtype, copy=False
            )
        return Dataset.from_array(
            window,
            no_data_value=nodata_sentinel,
            geo_ref=GeoReference(geo=cropped.geotransform, epsg=cropped.epsg),
        )

    def _check_bbox_overlaps(
        self,
        data: np.ndarray,
        geo: tuple[float, ...] | list[float],
    ) -> None:
        """Raise a helpful error when the request bbox misses the raster (M2).

        Cropping a non-overlapping bbox would otherwise yield an empty raster
        and write a 0-cell GeoTIFF. This pre-check names the user bbox and the
        raster's geographic extent so a swapped lat/lon, off-globe coordinate,
        or region mismatch is easy to spot. Per-date `_download_dataset`
        failures are caught and logged (M1), so a single miss never aborts a
        full batch.

        Args:
            data: The raster array; only its last two dims (rows, cols) are read.
            geo: The GDAL 6-tuple geotransform of `data`.

        Raises:
            ValueError: When the request bbox does not overlap the raster.
        """
        origin_x = float(geo[0])
        pix_x = float(geo[1])
        origin_y = float(geo[3])
        pix_y = -float(geo[5])  # positive

        rows, cols = data.shape[-2:]
        raster_east = origin_x + cols * pix_x
        raster_south = origin_y - rows * pix_y
        col_left = max(0, int(np.floor((self.space.west - origin_x) / pix_x)))
        col_right = min(cols, int(np.ceil((self.space.east - origin_x) / pix_x)))
        row_top = max(0, int(np.floor((origin_y - self.space.north) / pix_y)))
        row_bot = min(rows, int(np.ceil((origin_y - self.space.south) / pix_y)))

        if col_right <= col_left or row_bot <= row_top:
            raise ValueError(
                f"requested bbox lat=[{self.space.south}, "
                f"{self.space.north}] lon=[{self.space.west}, "
                f"{self.space.east}] does not overlap the source "
                f"raster (extent lat=[{raster_south}, {origin_y}] "
                f"lon=[{origin_x}, {raster_east}]). Check the bbox "
                "for swapped lat/lon, off-globe coordinates, or a "
                "dataset whose region doesn't cover the bbox."
            )

    @staticmethod
    def _output_filename(
        ds_key: str,
        dataset: ChcDataset,
        var: Variable,
        date: pd.Timestamp,
    ) -> str:
        """Build the canonical output filename for one clipped raster."""
        granularity = dataset.temporal_resolution
        if granularity == "annual":
            date_str = f"{date.year}"
        elif granularity in {
            "monthly",
            "monthly-climatology",
            "2-monthly",
            "3-monthly",
        }:
            date_str = f"{date.year}.{date.month:02d}"
        elif granularity == "6-hourly":
            date_str = f"{date.year}.{date.month:02d}.{date.day:02d}.{date.hour:02d}"
        else:
            date_str = f"{date.year}.{date.month:02d}.{date.day:02d}"
        return f"{ds_key}_{var.name}_{date_str}.tif"
