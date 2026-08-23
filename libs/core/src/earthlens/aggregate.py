"""Temporal aggregation of CDS-shaped NetCDF files into per-window GeoTIFFs.

Read a NetCDF whose first axis is time, group its samples by a pandas
offset alias (`"1D"`, `"7D"`, `"1MS"`, `"QS-DEC"`, ...), reduce each
group with one of mean / sum / min / max / std, and write one GeoTIFF
per window. The whole pipeline runs against pyramids primitives —
`pyramids.netcdf.NetCDF` for read + CF metadata, `pyramids.dataset.Dataset`
for write — plus numpy and pandas. No xarray.

The module sits at the top level of `earthlens` because the algorithm is
not specific to any backend: any CDS-shaped NetCDF works (ECMWF S3
exports, CDS retrieves, CDS-Beta retrieves, ...). The ECMWF backend
chains it via `ECMWF.download(aggregate=...)` for the
"download-and-aggregate-in-one-call" path; standalone callers use
`aggregate_netcdf` directly.

The two headline public symbols are :class:`AggregationConfig` (the
frozen request shape) and :func:`aggregate_netcdf` (the function). They
are also re-exported from `earthlens` so callers can write
`from earthlens.core import AggregationConfig, aggregate_netcdf`.

Two lower-level primitives are public as well, because the provider
distributions need them to implement their own per-window reductions over
outputs that are not CDS NetCDF cubes (ghsl reduces a stack of per-epoch
GeoTIFFs, and the same shape suits stac's per-date COGs and
sentinel_hub's rendered windows):

* :func:`window_groups` — bucket a `DatetimeIndex` into
  `(window_label, mask)` pairs by a pandas offset alias.
* :func:`reduce_time_axis` — reduce a `(time, y, x)` stack along axis 0
  with a named op, honouring `skipna` / `min_count`.

They are supported API, not internals: `earthlens-core` and the five
provider distributions version independently, so a provider importing an
underscore-private core symbol would break at runtime on any core release
that renamed it.

Examples:
    - Standalone aggregation: read a CDS NetCDF, write per-month
      GeoTIFFs to disk. `Catalog` is only consulted to resolve the
      `(dataset, code)` pair into the `Variable` row that drives
      `is_flux` and the output filename — no `ECMWF` instance is
      built:

        ```python
        >>> from earthlens.core import AggregationConfig, aggregate_netcdf  # doctest: +SKIP
        >>> from earthlens.ecmwf import Catalog  # doctest: +SKIP
        >>> spec = Catalog().get_variable(  # doctest: +SKIP
        ...     "reanalysis-era5-single-levels", "2m-temperature"
        ... )
        >>> results = aggregate_netcdf(  # doctest: +SKIP
        ...     "out/2m_temperature_reanalysis-era5-single-levels.nc",
        ...     spec,
        ...     AggregationConfig(freq="1MS", op="mean", out_dir="out/monthly"),
        ... )
        >>> for window_label, arr, target in results:  # doctest: +SKIP
        ...     print(window_label, arr.shape, target.name)

        ```
    - In-memory aggregation: pass `out_dir=None` to skip disk writes
      and inspect the per-window arrays directly:

        ```python
        >>> from earthlens.core import AggregationConfig, aggregate_netcdf  # doctest: +SKIP
        >>> from earthlens.ecmwf import Catalog  # doctest: +SKIP
        >>> spec = Catalog().get_variable(  # doctest: +SKIP
        ...     "reanalysis-era5-single-levels", "2m-temperature"
        ... )
        >>> results = aggregate_netcdf(  # doctest: +SKIP
        ...     "out/2m_temperature_reanalysis-era5-single-levels.nc",
        ...     spec,
        ...     AggregationConfig(freq="1D", op="auto"),
        ... )
        >>> first_label, first_array, first_path = results[0]  # doctest: +SKIP
        >>> first_path is None  # doctest: +SKIP
        True

        ```
    - Bundled with download via the ECMWF backend (single call
      retrieves and aggregates each variable):

        ```python
        >>> from earthlens.core import AggregationConfig  # doctest: +SKIP
        >>> from earthlens.earthlens import EarthLens  # doctest: +SKIP
        >>> earthlens = EarthLens(  # doctest: +SKIP
        ...     data_source="ecmwf",
        ...     temporal_resolution="daily",
        ...     start="2022-01-01",
        ...     end="2022-01-31",
        ...     variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
        ...     lat_lim=[4.0, 5.0],
        ...     lon_lim=[-75.0, -74.0],
        ...     path="out/era5",
        ... )
        >>> earthlens.download(  # doctest: +SKIP
        ...     aggregate=AggregationConfig(freq="1MS", op="mean"),
        ... )

        ```
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from earthlens.base.raster import close_quietly

if TYPE_CHECKING:
    from pyramids.netcdf import NetCDF

    from earthlens.ecmwf import Variable

__all__ = [
    "AggregatedWindow",
    "AggregationConfig",
    "OperationLiteral",
    "aggregate_netcdf",
    "iter_aggregate_netcdf",
    "reduce_time_axis",
    "window_groups",
]


_TIME_VAR_CANDIDATES: tuple[str, ...] = ("valid_time", "time")


def _read_time_axis(nc: NetCDF) -> pd.DatetimeIndex:
    """Return a NetCDF's time coordinate as a :class:`pandas.DatetimeIndex`.

    Tries each name in :data:`_TIME_VAR_CANDIDATES` in order
    (`"valid_time"` first to cover CDS-Beta NetCDFs, then `"time"`
    for legacy CDS). The first candidate that resolves to a non-empty
    list of date strings via
    :meth:`pyramids.netcdf.NetCDF.get_time_variable` is returned as a
    :class:`pandas.DatetimeIndex`.

    pyramids' :meth:`get_time_variable` already parses the CF
    `"<unit> since <epoch>"` units string for us through
    `create_time_conversion_func`; this helper just chooses the
    candidate name and converts the formatted strings back to
    timestamps with :func:`pandas.to_datetime`.

    Args:
        nc: An open :class:`pyramids.netcdf.NetCDF` instance pointed
            at the source file.

    Returns:
        pd.DatetimeIndex: One entry per timestep in the NetCDF's
        time dimension.

    Raises:
        KeyError: If none of :data:`_TIME_VAR_CANDIDATES` is present
            on the NetCDF as a parseable time variable.
    """
    for name in _TIME_VAR_CANDIDATES:
        time_strs = nc.get_time_variable(var_name=name)
        if time_strs:
            return pd.to_datetime(time_strs)
    raise KeyError(
        f"NetCDF has no recognised time variable; tried "
        f"{list(_TIME_VAR_CANDIDATES)!r}. Re-check the file's time "
        "dimension name and CF `units` attribute."
    )


_LEVEL_DIM_CANDIDATES: tuple[str, ...] = ("pressure_level", "level")


def _find_level_dim(nc: NetCDF) -> str | None:
    """Return the pressure-level dimension name, or `None` for 3-D files.

    Walks `nc.dimension_names` looking for any of
    :data:`_LEVEL_DIM_CANDIDATES`. CDS pressure-level NetCDFs use
    `pressure_level`; some derived datasets use plain `level`. The
    first match wins.

    Args:
        nc: An open :class:`pyramids.netcdf.NetCDF` instance — either
            the root MDIM container or a variable subset returned by
            :meth:`NetCDF.get_variable`. Both surfaces expose the
            full dim list under `dimension_names`.

    Returns:
        str | None: The matched dimension name when the NetCDF has a
        pressure-level axis; `None` for 3-D `(time, lat, lon)` files.
    """
    dim_names = nc.dimension_names or ()
    for candidate in _LEVEL_DIM_CANDIDATES:
        if candidate in dim_names:
            return candidate
    return None


def _resolve_pressure_level(
    nc: NetCDF,
    level: int | float | None,
) -> NetCDF:
    """Pin a pressure level on a 4-D NetCDF, or pass through 3-D files.

    Decision matrix:

    | Has level dim? | `level` set? | Result                         |
    |----------------|--------------|--------------------------------|
    | Yes            | Yes          | `nc.sel(<level_dim>=level)`    |
    | Yes            | No           | `ValueError` (ambiguous)       |
    | No             | Yes          | `ValueError` (no level to set) |
    | No             | No           | Pass-through                   |

    Args:
        nc: The NetCDF to examine.
        level: The :attr:`AggregationConfig.level` value.

    Returns:
        NetCDF: Either the original `nc` (3-D pass-through) or a new
        instance from :meth:`pyramids.netcdf.NetCDF.sel` with the
        chosen level pinned.

    Raises:
        ValueError: When the NetCDF has a pressure-level dimension
            but no `level` was passed, or when `level` was passed but
            the NetCDF has no pressure-level dimension.
    """
    level_dim = _find_level_dim(nc)
    if level_dim is None and level is None:
        return nc
    if level_dim is None and level is not None:
        raise ValueError(
            f"`level={level!r}` was set but the NetCDF has no "
            f"pressure-level dimension (looked for "
            f"{list(_LEVEL_DIM_CANDIDATES)!r} in "
            f"{list(nc.dimension_names or ())!r}). Drop `level` or "
            "point at a 4-D pressure-level file."
        )
    if level_dim is not None and level is None:
        raise ValueError(
            f"NetCDF has a {level_dim!r} dimension; pass "
            "`level=<value>` on AggregationConfig to pin one (e.g. "
            "`level=1000`). 4-D aggregation across all levels at "
            "once is not supported."
        )
    assert level_dim is not None  # narrowed by the branches above
    return nc.sel(**{level_dim: level})


def window_groups(
    time_axis: pd.DatetimeIndex,
    freq: str,
) -> Iterator[tuple[pd.Timestamp, np.ndarray]]:
    """Yield `(window_label, mask)` pairs that bucket `time_axis` by `freq`.

    Builds a `pandas.Series` indexed by `time_axis` and groups it
    with `pandas.Grouper(freq=freq)`. Each group's `index` gives the
    timestamps belonging to that window; the boolean mask is built
    by membership against `time_axis` so callers can use it to slice
    a numpy array along its first axis.

    Empty groups (windows with no samples) are silently skipped —
    `aggregate_netcdf` doesn't write a GeoTIFF for a window it has
    no data for.

    Args:
        time_axis: Time coordinate as a :class:`pandas.DatetimeIndex`.
            Typically the result of :func:`_read_time_axis`.
        freq: Pandas offset alias (`"1D"`, `"7D"`, `"1MS"`, `"QS-DEC"`,
            `"AS"`, ...). Anything :class:`pandas.Grouper` accepts.

    Yields:
        tuple[pd.Timestamp, np.ndarray]: For each non-empty window:
        the group key (window's left-edge timestamp) paired with a
        boolean mask of length `len(time_axis)`.

    Examples:
        - Group four 6-hourly slots into one daily window:

            ```python
            >>> import pandas as pd
            >>> from earthlens.aggregate import window_groups
            >>> idx = pd.date_range("2022-01-01", periods=4, freq="6h")
            >>> windows = list(window_groups(idx, "1D"))
            >>> len(windows)
            1
            >>> label, mask = windows[0]
            >>> label
            Timestamp('2022-01-01 00:00:00')
            >>> mask.tolist()
            [True, True, True, True]

            ```
        - Group two days of 6-hourly samples into two daily windows:

            ```python
            >>> import pandas as pd
            >>> from earthlens.aggregate import window_groups
            >>> idx = pd.date_range("2022-01-01", periods=8, freq="6h")
            >>> [label.strftime("%Y-%m-%d") for label, _ in window_groups(idx, "1D")]
            ['2022-01-01', '2022-01-02']

            ```
    """
    indexer = pd.Series(np.arange(len(time_axis)), index=time_axis)
    timestamps = pd.Index(time_axis)
    for window_label, group in indexer.groupby(pd.Grouper(freq=freq)):
        if group.empty:
            continue
        mask = np.asarray(timestamps.isin(group.index))
        yield window_label, mask


OperationLiteral = Literal["mean", "sum", "min", "max", "std", "auto"]


_REDUCERS_SKIPNA: dict[str, Callable[..., Any]] = {
    "mean": np.nanmean,
    "sum": np.nansum,
    "min": np.nanmin,
    "max": np.nanmax,
    "std": np.nanstd,
}

_REDUCERS_STRICT: dict[str, Callable[..., Any]] = {
    "mean": np.mean,
    "sum": np.sum,
    "min": np.min,
    "max": np.max,
    "std": np.std,
}


def reduce_time_axis(
    arr: np.ndarray,
    op: str,
    skipna: bool,
    min_count: int | None,
) -> np.ndarray:
    """Reduce a `(time, lat, lon)` slice along axis 0 with the named op.

    Dispatches `op` to the matching numpy reducer (`np.nanmean` etc.
    when `skipna=True`, plain `np.mean` etc. when `skipna=False`),
    then masks pixels whose non-NaN sample count falls below
    `min_count`.

    `op="auto"` is **not** accepted here — `aggregate_netcdf` resolves
    `auto` to a concrete operator before calling this helper. Passing
    `"auto"` raises `KeyError` to surface the mistake at the call site.

    Args:
        arr: Array to reduce. The first axis is collapsed; the
            remaining axes pass through unchanged. Typically
            `(N_in_window, lat, lon)`.
        op: One of `"mean" / "sum" / "min" / "max" / "std"`. Resolved
            to a numpy reducer via the dispatch table.
        skipna: When `True`, the NaN-aware reducer is used
            (`np.nanmean` etc.); when `False`, the strict variant is
            used and any NaN in a window propagates to the output.
        min_count: When set, pixels with fewer than this many non-NaN
            samples along axis 0 emit NaN regardless of the reduction
            result. `None` disables the floor.

    Returns:
        np.ndarray: Reduced array with axis 0 collapsed.

    Raises:
        KeyError: If `op` is not in the dispatch table (in particular,
            `"auto"` is rejected — resolve it to a concrete op first).

    Examples:
        - NaN-aware mean over the time axis:

            ```python
            >>> import numpy as np
            >>> from earthlens.aggregate import reduce_time_axis
            >>> arr = np.array([[[1.0, 2.0]], [[3.0, np.nan]], [[5.0, 6.0]]])
            >>> reduce_time_axis(arr, op="mean", skipna=True, min_count=None).tolist()
            [[3.0, 4.0]]

            ```
        - Strict mean propagates NaN when `skipna=False`:

            ```python
            >>> import numpy as np
            >>> from earthlens.aggregate import reduce_time_axis
            >>> arr = np.array([[[1.0, np.nan]], [[3.0, 4.0]]])
            >>> result = reduce_time_axis(arr, op="mean", skipna=False, min_count=None)
            >>> bool(np.isnan(result[0, 1])), float(result[0, 0])
            (True, 2.0)

            ```
        - `min_count` masks under-sampled pixels:

            ```python
            >>> import numpy as np
            >>> from earthlens.aggregate import reduce_time_axis
            >>> arr = np.array([[[1.0, np.nan]], [[2.0, np.nan]]])
            >>> result = reduce_time_axis(arr, op="mean", skipna=True, min_count=2)
            >>> float(result[0, 0]), bool(np.isnan(result[0, 1]))
            (1.5, True)

            ```
    """
    table = _REDUCERS_SKIPNA if skipna else _REDUCERS_STRICT
    if op not in table:
        raise KeyError(
            f"unknown reduction op {op!r}; expected one of "
            f"{sorted(table)!r} (resolve 'auto' before calling reduce_time_axis)"
        )
    reducer = table[op]
    result = reducer(arr, axis=0)
    if min_count is not None:
        non_nan_count = np.count_nonzero(~np.isnan(arr), axis=0)
        result = np.where(non_nan_count >= min_count, result, np.nan)
    return np.asarray(result)


def _resolve_op(op: OperationLiteral, var_info: Variable) -> str:
    """Turn `op="auto"` into a concrete reduction based on the catalog row.

    `Variable.is_flux` (in `earthlens.ecmwf.catalog`) is `True` for CDS
    flux variables — precipitation, evaporation, runoff, radiation
    accumulations — and `False` for state variables (temperature,
    pressure, humidity, ...).

    Resolution rules (first match wins):

    * `op="auto"` + `var_info.is_pre_aggregated=True` → `"mean"`
    * `op="auto"` + `var_info.is_flux=True` → `"sum"`
    * `op="auto"` + `var_info.is_flux=False` → `"mean"`
    * any explicit op → returned unchanged

    `is_pre_aggregated` wins over `is_flux`: a flux variable from a
    `derived-era5-*-daily-statistics` / `reanalysis-era5-*-monthly-means`
    dataset is already a server-side daily / monthly aggregate, so `"auto"`
    resolves to `"mean"` — a plain `"sum"` would re-accumulate the aggregates
    and multiply by the number of samples per window (~30× for a monthly
    window over daily statistics). `is_pre_aggregated` is read defensively
    (`getattr`, default `False`) so a `var_info` without it behaves as before.

    This **replaces** the legacy `mean × days_later` scaling that
    `examples/post_process_ecmwf_netcdf.py:226` (pre-rewrite) used.
    The two are equivalent only when every slot inside a window has
    a sample; for partial windows, true `sum` is correct and
    `mean × N` overcounts. `reduce_time_axis(..., op="sum", ...)` gives the
    correct per-window total in both cases.

    Args:
        op: The :attr:`AggregationConfig.op` value, possibly `"auto"`.
        var_info: Catalog entry for the variable being aggregated.
            Only `is_pre_aggregated` (if present) and `is_flux` are
            consulted; the rest is ignored.

    Returns:
        str: The concrete operator name (`"mean"`, `"sum"`, `"min"`,
        `"max"`, or `"std"`) ready for :func:`reduce_time_axis`.

    Examples:
        - State variable with `is_flux=False` resolves to `"mean"`:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _resolve_op
            >>> _resolve_op("auto", SimpleNamespace(is_flux=False))
            'mean'

            ```
        - Flux variable with `is_flux=True` resolves to `"sum"`:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _resolve_op
            >>> _resolve_op("auto", SimpleNamespace(is_flux=True))
            'sum'

            ```
        - A pre-aggregated flux variable resolves to `"mean"`, not `"sum"`:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _resolve_op
            >>> _resolve_op(
            ...     "auto",
            ...     SimpleNamespace(is_flux=True, is_pre_aggregated=True),
            ... )
            'mean'

            ```
        - Explicit ops pass through verbatim:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _resolve_op
            >>> _resolve_op("max", SimpleNamespace(is_flux=True))
            'max'

            ```
    """
    if op != "auto":
        return op
    if getattr(var_info, "is_pre_aggregated", False):
        return "mean"
    return "sum" if var_info.is_flux else "mean"


class AggregationConfig(BaseModel):
    """Frozen request shape consumed by :func:`aggregate_netcdf`.

    Carries the windowing frequency, reduction operator, and output
    location. Frozen + `extra="forbid"` so a typo in a field name
    (e.g. `freqency=`) fails loud at construction time rather than
    silently using the default.

    Attributes:
        freq: Pandas offset alias defining the window. Examples:
            `"1D"` (daily), `"7D"` (weekly), `"1MS"` (month-start),
            `"QS-DEC"` (DJF/MAM/JJA/SON climatological seasons),
            `"AS"` (annual). Any string accepted by
            `pandas.Grouper(freq=...)` is valid.
        op: Reduction applied within each window. `"auto"` reads
            `Variable.is_flux` (state→`"mean"`, flux→`"sum"`); the
            other values are forwarded as-is to the dispatcher.
        out_dir: Directory the per-window GeoTIFFs are written to.
            Created (with parents) if absent. `None` skips the write
            step entirely and returns arrays in memory only.
        cell_size: Pixel size in degrees, informational only. `0.125`
            for ERA5 native, `0.1` for ERA5-Land. The geotransform
            written to each GeoTIFF is read off the NetCDF, not from
            this value, and it is not encoded in the filename.
        level: When the NetCDF has a `pressure_level` dimension, pin
            this level via :meth:`pyramids.netcdf.NetCDF.sel`. `None`
            (default) requires a 3-D NetCDF; pass an explicit level
            (e.g. `1000`) to aggregate a single 4-D layer.
        skipna: When `True`, the reduction is NaN-aware
            (`np.nanmean` etc.). `False` propagates any NaN in a
            window to the output.
        min_count: Minimum non-NaN samples required for a window to
            produce a non-NaN value. Windows with fewer samples emit
            NaN. `None` (default) means no minimum.

    Examples:
        - Daily-mean defaults — only `freq` is required, the rest
          stays at sensible CDS-shaped defaults:

            ```python
            >>> from earthlens.aggregate import AggregationConfig
            >>> cfg = AggregationConfig(freq="1D")
            >>> cfg.op
            'auto'
            >>> cfg.skipna
            True
            >>> cfg.cell_size
            0.125
            >>> cfg.out_dir is None
            True

            ```
        - Monthly sum into an explicit output directory:

            ```python
            >>> from pathlib import Path
            >>> from earthlens.aggregate import AggregationConfig
            >>> cfg = AggregationConfig(
            ...     freq="1MS",
            ...     op="sum",
            ...     out_dir=Path("out") / "monthly",
            ... )
            >>> cfg.freq, cfg.op
            ('1MS', 'sum')
            >>> cfg.out_dir.name
            'monthly'

            ```
        - Pin a pressure level for 4-D inputs and require a minimum
          sample count per window:

            ```python
            >>> from earthlens.aggregate import AggregationConfig
            >>> cfg = AggregationConfig(
            ...     freq="7D", op="mean", level=1000, min_count=20,
            ... )
            >>> cfg.level, cfg.min_count
            (1000, 20)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    freq: str
    op: OperationLiteral = "auto"
    out_dir: Path | None = None
    cell_size: float = 0.125
    level: int | float | None = None
    skipna: bool = True
    min_count: int | None = None
    keep_arrays: bool = True


@dataclass(frozen=True, eq=False)
class AggregatedWindow:
    """One reduced time window: its label, its array, and where it was written.

    Yielded by :func:`iter_aggregate_netcdf`. `array` is `None` when the
    request set `keep_arrays=False` and the window was written to disk — the
    point of that mode is that a long run does not accumulate every window in
    memory alongside the GeoTIFFs it has already produced.

    Comparison is left as identity (`eq=False`). A generated `__eq__` would
    compare the `array` field with `==`, which for a numpy array yields an
    elementwise array and then raises `ValueError: truth value ... ambiguous`;
    the matching `__hash__` would raise `TypeError` because an ndarray is
    unhashable. Compare the fields you actually care about instead.

    Attributes:
        label: The window's left-edge timestamp.
        array: The reduced 2-D array, or `None` when the request asked not to
            retain it.
        path: The written GeoTIFF, or `None` when `out_dir` was `None`.

    Examples:
        - A window keeps both its array and its path when it was written:
            ```python
            >>> import numpy as np
            >>> import pandas as pd
            >>> from pathlib import Path
            >>> from earthlens.aggregate import AggregatedWindow
            >>> window = AggregatedWindow(
            ...     label=pd.Timestamp("2020-01-01"),
            ...     array=np.array([[1.0, 2.0]]),
            ...     path=Path("out/t2m_1D_20200101.tif"),
            ... )
            >>> window.label.strftime("%Y-%m-%d")
            '2020-01-01'
            >>> float(window.array.mean())
            1.5
            >>> window.path.name
            't2m_1D_20200101.tif'

            ```
        - A discarded array leaves only the label and the path to read back:
            ```python
            >>> import pandas as pd
            >>> from pathlib import Path
            >>> from earthlens.aggregate import AggregatedWindow
            >>> window = AggregatedWindow(
            ...     label=pd.Timestamp("2020-02-01"),
            ...     array=None,
            ...     path=Path("out/t2m_1D_20200201.tif"),
            ... )
            >>> window.array is None
            True
            >>> window.path.name
            't2m_1D_20200201.tif'

            ```
    """

    label: pd.Timestamp
    array: np.ndarray | None
    path: Path | None


def _output_stem(var_info: Variable) -> str:
    """Filename stem for a variable's aggregated GeoTIFF windows.

    `<cds_variable>_<dataset_id or cds_dataset>` when the row carries either id,
    mirroring the ECMWF backend's `.nc` naming so two datasets that share a
    `cds_variable` never collide in one `out_dir` — whether that is two ordinary
    datasets (ERA5 single-levels vs ERA5-Land `total_precipitation`) or the two
    curated GloFAS streams (`cems-glofas-historical` consolidated vs
    `-intermediate`, which also share `cds_dataset` and are told apart by
    `dataset_id`). Falls back to the bare `cds_variable` for a `var_info` that
    carries neither id — the s3 / erddap adapters — leaving those backends'
    filenames unchanged.

    Args:
        var_info: The catalog row being aggregated. Read structurally
            (`cds_variable`, and the optional `dataset_id` / `cds_dataset`), so a
            row from any backend works.

    Returns:
        The filename stem, without the trailing `_<freq>_<window>.tif`.

    Examples:
        - An ECMWF row appends its dataset id (`dataset_id` == `cds_dataset` for
          an ordinary row):

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _output_stem
            >>> era5 = SimpleNamespace(
            ...     cds_variable="total_precipitation",
            ...     cds_dataset="reanalysis-era5-single-levels",
            ...     dataset_id="reanalysis-era5-single-levels",
            ... )
            >>> _output_stem(era5)
            'total_precipitation_reanalysis-era5-single-levels'

            ```
        - A curated override (a `dataset_id` differing from `cds_dataset`) uses
          the `dataset_id`, so two configs of one dataset stay distinct:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _output_stem
            >>> glofas = SimpleNamespace(
            ...     cds_variable="average_river_discharge_in_the_last_24_hours",
            ...     cds_dataset="cems-glofas-historical",
            ...     dataset_id="cems-glofas-historical-intermediate",
            ... )
            >>> _output_stem(glofas)
            'average_river_discharge_in_the_last_24_hours_cems-glofas-historical-intermediate'

            ```
        - A backend row carrying neither id keeps the bare variable name:

            ```python
            >>> from types import SimpleNamespace
            >>> from earthlens.aggregate import _output_stem
            >>> _output_stem(SimpleNamespace(cds_variable="elevation"))
            'elevation'

            ```
    """
    stem: str = var_info.cds_variable
    dataset = getattr(var_info, "dataset_id", None) or getattr(
        var_info, "cds_dataset", None
    )
    if dataset:
        stem = f"{stem}_{dataset}"
    return stem


def aggregate_netcdf(
    nc_path: Path | str,
    var_info: Variable,
    config: AggregationConfig,
) -> list[tuple[pd.Timestamp, np.ndarray | None, Path | None]]:
    """Slice a CDS-shaped NetCDF into per-window aggregated outputs.

    Reads the NetCDF, groups its time axis by `config.freq`, reduces
    each group with `config.op`, and (when `config.out_dir` is set)
    writes one GeoTIFF per window. Returns the per-window arrays
    alongside their timestamps and output paths so callers can chain
    further processing without re-opening the files.

    Args:
        nc_path: Path to the NetCDF on disk.
        var_info: Catalog row for the variable being aggregated. Used
            to pick the variable from the NetCDF
            (`var_info.nc_variable`), seed the output filename
            (`var_info.cds_variable`, plus `dataset_id` for a curated
            override — see :func:`_output_stem`), and resolve `op="auto"`
            (`var_info.is_flux`).
        config: Frozen :class:`AggregationConfig` describing the
            window, reduction, and output location.

    Returns:
        list[tuple[pd.Timestamp, np.ndarray | None, Path | None]]: One
        entry per window. The first item is the window's left-edge
        timestamp; the second is the reduced 2-D array — `None` only when
        the request set `keep_arrays=False` and the window was written to
        disk; the third is the GeoTIFF path (or `None` when
        `config.out_dir` was `None`).

    Raises:
        KeyError: If the NetCDF has no recognised time variable
            (`valid_time` / `time`); see :func:`_read_time_axis`.
        ValueError: If `config.level` is set but the NetCDF has no
            pressure-level dimension, or vice versa; see
            :func:`_resolve_pressure_level`. Also raised by pandas
            when `config.freq` is not a recognised offset alias.

    See Also:
        - :class:`AggregationConfig`: the frozen request payload.
        - :class:`earthlens.ecmwf.Catalog`: resolves `(dataset, code)`
          pairs to the :class:`earthlens.ecmwf.Variable` rows that
          drive `var_info.is_flux` and the output filename.
        - `examples/post_process_ecmwf_netcdf.py`: thin CLI demo of
          this function (after task L1).
    """
    return [
        (window.label, window.array, window.path)
        for window in iter_aggregate_netcdf(nc_path, var_info, config)
    ]


def iter_aggregate_netcdf(
    nc_path: Path | str,
    var_info: Variable,
    config: AggregationConfig,
) -> Iterator[AggregatedWindow]:
    """Yield one :class:`AggregatedWindow` per time window, streaming.

    The streaming counterpart to :func:`aggregate_netcdf`, and the
    implementation it is built on. Two properties make it usable on cubes
    that do not fit in memory:

    * **Only one window is resident at a time.** The time steps for the
      current window are read band by band and stacked; the whole
      `(time, y, x)` cube is never materialised. A ten-year hourly ERA5
      request over Europe at 0.25° is ~33.6 GB as one array but ~9 MB per
      daily window.
    * **Windows are not accumulated.** Each is yielded and then dropped, so
      a caller that writes and discards holds nothing. Pair it with
      `keep_arrays=False` to drop the reduced array too once it is on disk.

    Every handle opened — the container, the variable subset, and the
    level-pinned view — is closed when the generator finishes or is
    abandoned, so the file can be deleted or overwritten straight after.
    Closing the container alone is not sufficient: the variable subset holds
    its own handle, and one left open keeps a Windows lock on the file.

    Args:
        nc_path: Path to the NetCDF on disk.
        var_info: Catalog row for the variable being aggregated. Used to
            pick the variable from the NetCDF (`var_info.nc_variable`),
            seed the output filename (`_output_stem` — `var_info.cds_variable`,
            plus `dataset_id` for a curated override), and resolve
            `op="auto"` (`var_info.is_flux`).
        config: Frozen :class:`AggregationConfig` describing the window,
            reduction, output location, and whether to retain arrays.

    Yields:
        AggregatedWindow: One per window, in time order.

    Raises:
        KeyError: If the NetCDF has no recognised time variable
            (`valid_time` / `time`); see :func:`_read_time_axis`.
        ValueError: If `config.level` is set but the NetCDF has no
            pressure-level dimension, or vice versa; see
            :func:`_resolve_pressure_level`. Also raised by pandas when
            `config.freq` is not a recognised offset alias.

    See Also:
        - :func:`aggregate_netcdf`: the eager `list` form of this function.
        - :class:`AggregationConfig`: the frozen request payload.
    """
    from pyramids.dataset import Dataset
    from pyramids.netcdf import NetCDF

    out_dir: Path | None = config.out_dir
    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    op = _resolve_op(config.op, var_info)

    nc = NetCDF.read_file(str(nc_path))
    # Every handle opened here is closed on the way out. Closing only the
    # root container is not enough: the variable subset (and the level-pinned
    # `sel()` result) each hold their own handle, and any one of them left
    # open keeps a lock on the file under Windows — so the caller could not
    # delete or overwrite the NetCDF it just aggregated.
    opened: list[Any] = [nc]
    try:
        # Read time axis + geotransform from the root container — only the
        # container exposes `get_time_variable` against the underlying CF
        # metadata. The variable-subset cube returned by `get_variable`
        # tracks coords on `_band_dim_values_map` instead, but does not
        # round-trip them through `get_time_variable`. The cube is what
        # `sel()` and the band-dim-aware multi-D logic need, so use it
        # for level pinning + array read.
        time_axis = _read_time_axis(nc)
        geo = nc.geotransform
        var = nc.get_variable(var_info.nc_variable)
        opened.append(var)
        var = _resolve_pressure_level(var, config.level)
        if var is not opened[-1]:
            opened.append(var)

        stem = _output_stem(var_info)
        for window_label, mask in window_groups(time_axis, config.freq):
            slice_ = _read_window(var, mask)
            reduced = reduce_time_axis(
                slice_, op=op, skipna=config.skipna, min_count=config.min_count
            )

            target: Path | None = None
            if out_dir is not None:
                target = out_dir / (f"{stem}_{config.freq}_{window_label:%Y%m%d}.tif")
                Dataset.create_from_array(arr=reduced, geo=geo, epsg=4326).to_file(
                    str(target)
                )

            keep = config.keep_arrays or target is None
            yield AggregatedWindow(
                label=window_label,
                array=reduced if keep else None,
                path=target,
            )
    finally:
        for handle in reversed(opened):
            close_quietly(handle)


def _read_window(var: Any, mask: np.ndarray) -> np.ndarray:
    """Read just the time steps `mask` selects, stacked as `(time, y, x)`.

    Reads band by band and stacks, rather than reading the whole cube and
    slicing it: the peak allocation is then one window, not the entire time
    series. `read_array(band=...)` is 0-based and returns the 2-D grid for
    that step.

    Args:
        var: The (optionally level-pinned) variable cube. Typed loosely
            because `get_variable` returns the same class as the container but
            only the `read_array` surface is used here.
        mask: Boolean mask over the time axis selecting this window's steps.

    Returns:
        numpy.ndarray: The window's steps stacked along a leading time axis,
            in the widest dtype any contributing band needed.

    Raises:
        ValueError: If `mask` selects no time steps.
    """
    indices = np.flatnonzero(mask)
    # `window_groups` never yields an all-False mask, so an empty window would
    # be a caller bug; say so rather than returning a shape that silently
    # loses the grid and breaks the reduction downstream.
    if indices.size == 0:
        raise ValueError(
            "_read_window received a mask selecting no time steps; "
            "window_groups only yields non-empty windows."
        )
    # Fill a pre-sized buffer rather than `np.stack`ing a list: stacking holds
    # every band plus the assembled copy at once, doubling the window's peak.
    first = np.asarray(var.read_array(band=int(indices[0])))
    out = np.empty((indices.size, *first.shape), dtype=first.dtype)
    out[0] = first
    del first
    for position, index in enumerate(indices[1:], start=1):
        band = np.asarray(var.read_array(band=int(index)))
        if band.dtype != out.dtype:
            # A band whose dtype differs from the first would be cast
            # silently by the assignment below — e.g. float64 truncated into
            # a float32 buffer. Widen instead of losing precision.
            out = out.astype(np.promote_types(out.dtype, band.dtype))
        out[position] = band
    return out
