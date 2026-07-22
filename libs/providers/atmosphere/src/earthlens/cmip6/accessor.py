"""Thin pyramids accessor for the CMIP6 Zarr stores (the read boundary).

Everything that opens a `gs://cmip6` Zarr store, windows it, and writes a NetCDF
subset lives here — the one place earthlens touches pyramids for CMIP6. earthlens
itself never imports `xarray` / `zarr` / `gcsfs`; pyramids reads the store through
GDAL's `/vsigs/` multidimensional driver (no gcsfs needed), and this module only:

* rewrites a `gs://cmip6/<path>/` `zstore` URI to the GDAL `ZARR:"/vsigs/..."`
  form (:func:`zstore_to_vsi`);
* forces **anonymous** GCS access with `GS_NO_SIGN_REQUEST` around every read
  (:func:`anonymous_gcs`) — pyramids' `anon=True` only sets the AWS flag, and
  this machine may carry ambient GCS credentials, so the flag is set explicitly;
* maps a CF `[start, end]` **date window to an integer time-index range**
  (:func:`resolve_time_window`) — the gridded reader selects time by integer
  index, and `LabeledDataset.select_time` is the public path that decodes CF
  time / non-standard calendars;
* reads the gridded `(time, bbox)` slice and writes it to NetCDF
  (:func:`write_subset`), a windowed read that fetches only the requested cells.

The pyramids reader classes are imported lazily behind an install-hint so the
package imports (and the backend constructs) without a read ever happening.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from earthlens.cmip6.resolver import ResolvedStore

#: GDAL env flag that forces anonymous (unsigned) access to a public GCS bucket.
GS_NO_SIGN_ENV = "GS_NO_SIGN_REQUEST"

#: Minimum pyramids version whose `NetCDF.subset` reads a windowed slice of a
#: remote CF/GeoZarr store — named in the install hint.
_PYRAMIDS_HINT = (
    "the CMIP6 backend reads Zarr through pyramids; install "
    "`pip install 'pyramids-gis>=0.38.0'`."
)


@contextlib.contextmanager
def anonymous_gcs() -> Iterator[None]:
    """Force anonymous GCS access for the duration of the `with` block.

    Sets `GS_NO_SIGN_REQUEST=YES` so GDAL's `/vsigs/` driver reads the public
    `gs://cmip6` bucket without signing — even when the environment carries GCS
    credentials (e.g. a Google Earth Engine service account) — then restores the
    prior value. The flag must stay live for the lazy data-chunk reads, not just
    the open, so wrap the whole read + write.

    Yields:
        None: control to the `with` body.
    """
    previous = os.environ.get(GS_NO_SIGN_ENV)
    os.environ[GS_NO_SIGN_ENV] = "YES"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(GS_NO_SIGN_ENV, None)
        else:
            os.environ[GS_NO_SIGN_ENV] = previous


def zstore_to_vsi(zstore: str) -> str:
    """Rewrite a `gs://` store URI to the GDAL `ZARR:"/vsigs/..."` form.

    Args:
        zstore: A `gs://cmip6/<path>/` store URI (or an already-rewritten
            `/vsigs/...` path).

    Returns:
        str: The `ZARR:"/vsigs/<bucket>/<path>/"` path GDAL's multidim Zarr
            driver opens.

    Raises:
        ValueError: If `zstore` is neither a `gs://` URI nor a `/vsigs/` path.

    Examples:
        - Rewrite a store URI:
            ```python
            >>> from earthlens.cmip6.accessor import zstore_to_vsi
            >>> zstore_to_vsi("gs://cmip6/CMIP6/ScenarioMIP/x/tas/gn/v1/")
            'ZARR:"/vsigs/cmip6/CMIP6/ScenarioMIP/x/tas/gn/v1/"'

            ```
    """
    if zstore.startswith("gs://"):
        vsi = "/vsigs/" + zstore[len("gs://") :]
    elif zstore.startswith("/vsigs/"):
        vsi = zstore
    elif zstore.startswith('ZARR:"'):
        return zstore
    else:
        raise ValueError(
            f"cannot rewrite {zstore!r} to a GDAL /vsigs/ path: expected a "
            "'gs://' store URI."
        )
    return f'ZARR:"{vsi}"'


def _netcdf_reader() -> Any:
    """Return the pyramids `NetCDF` class (lazy import) for gridded reads.

    Returns:
        The `pyramids.netcdf.NetCDF` class.

    Raises:
        ImportError: When pyramids is unavailable (names the minimum version).
    """
    try:
        from pyramids.netcdf import NetCDF
    except ImportError as exc:  # pragma: no cover - pyramids is a core dep
        raise ImportError(_PYRAMIDS_HINT) from exc
    return NetCDF


def _labeled_reader() -> Any:
    """Return the pyramids `LabeledDataset` class (lazy import) for time reads.

    Returns:
        The `pyramids.netcdf.LabeledDataset` class.

    Raises:
        ImportError: When pyramids is unavailable.
    """
    try:
        from pyramids.netcdf import LabeledDataset
    except ImportError as exc:  # pragma: no cover - pyramids is a core dep
        raise ImportError(_PYRAMIDS_HINT) from exc
    return LabeledDataset


def resolve_time_window(
    zstore: str,
    variable: str,
    start: Any = None,
    end: Any = None,
    *,
    time_dim: str = "time",
) -> tuple[int, int] | None:
    """Map a CF `[start, end]` date window to an integer time-index range.

    The gridded reader (:func:`write_subset`) selects time by **integer index**;
    CMIP6 ARCO stores do not surface CF time units through GDAL's multidim path,
    but `LabeledDataset.select_time` decodes the store's own time `units` +
    `calendar` (so `noleap` / `360_day` work). The half-open window is recovered
    from public counts alone:

    * `i0 = N - select_time(start=start).sizes[time]` (steps at or after `start`)
    * `i1 = select_time(end=end).sizes[time]` (steps at or before `end`)

    Args:
        zstore: The `gs://cmip6/...` store URI.
        variable: The data variable to open (keeps the read light).
        start: Inclusive window start (`datetime` / `"YYYY-MM-DD"` / `None`).
        end: Inclusive window end; `None` runs to the last step.
        time_dim: Name of the time dimension.

    Returns:
        tuple[int, int] | None: The half-open `(i0, i1)` index range, or `None`
            when neither bound is given (the caller should read the whole series).

    Raises:
        ValueError: If the window selects no timesteps.
    """
    if start is None and end is None:
        return None
    labeled = _labeled_reader()
    with anonymous_gcs():
        # engine="zarr" forces the Zarr driver explicitly: a CMIP6 store URI ends
        # in /v<YYYYMMDD>/ (never `.zarr`), so pyramids' suffix-based auto-detect
        # would otherwise open it down the NetCDF branch — asymmetric with
        # write_subset, which opens the same store through the ZARR: /vsigs/ path.
        dataset = labeled.read_file(
            zstore, variables=[variable], anon=True, engine="zarr"
        )
        try:
            total = int(dataset.sizes.get(time_dim, 0))
            if total == 0:
                return None
            i0 = (
                0
                if start is None
                else total
                - int(
                    dataset.select_time(start=start, time_dim=time_dim).sizes[time_dim]
                )
            )
            i1 = (
                total
                if end is None
                else int(
                    dataset.select_time(end=end, time_dim=time_dim).sizes[time_dim]
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"no CMIP6 timesteps fall in the window [{start}, {end}] for "
                f"{variable!r} in {zstore!r}: {exc}"
            ) from exc
        finally:
            _close_quietly(dataset)
    if i1 <= i0:
        raise ValueError(
            f"no CMIP6 timesteps fall in the window [{start}, {end}] for "
            f"{variable!r} in {zstore!r}."
        )
    return (i0, i1)


def write_subset(
    zstore: str,
    variable: str,
    *,
    bbox: tuple[float, float, float, float] | None,
    time: int | tuple[int, int] | slice | None,
    out_path: Path | str,
    crs: int | str = 4326,
) -> Path:
    """Read a `(variable, time, bbox)` window of a store and write it to NetCDF.

    Opens the resolved Zarr store through pyramids' `NetCDF` reader (GDAL
    `/vsigs/`, anonymous) and writes the windowed slice — only the requested
    cells are fetched. The read + write both run inside :func:`anonymous_gcs`.

    Args:
        zstore: The `gs://cmip6/...` store URI.
        variable: The data variable to read (`"tas"`).
        bbox: `(west, south, east, north)` crop in `crs`, or `None` for the full
            grid.
        time: Integer time selector (`int` / `(start, stop)` / `slice` / `None`).
            `None` is valid only when the time dimension has length 1.
        out_path: Destination path for the written NetCDF.
        crs: CRS of `bbox`. Defaults to `4326` (lon/lat).

    Returns:
        Path: The written NetCDF path.
    """
    netcdf = _netcdf_reader()
    vsi = zstore_to_vsi(zstore)
    out = Path(out_path)
    with anonymous_gcs():
        container = netcdf.read_file(vsi)
        try:
            subset = container.subset(variable, time=time, bbox=bbox, crs=crs)
            subset.to_file(str(out))
        finally:
            _close_quietly(container)
    return out


def _close_quietly(handle: Any) -> None:
    """Close a pyramids reader handle, ignoring any error.

    Releasing the handle lets the store be reopened on Windows (where an open
    handle can block a later open / unlink).

    Args:
        handle: An opened `NetCDF` / `LabeledDataset`.
    """
    try:
        handle.close()
    except Exception:  # noqa: BLE001 - best-effort handle release  # nosec B110
        pass


def store_output_stem(store: ResolvedStore, start: Any, end: Any) -> str:
    """Compose a unique output-file stem for one resolved store + window.

    The store `version` is folded in so two calls that pin different explicit
    `version=` values for the same identity (or a CSV carrying duplicate
    `(identity, version)` rows) write to distinct files instead of the second
    silently overwriting the first.

    Args:
        store: The resolved store (supplies the facet slug + version).
        start: Window start (its `%Y%m%d` is appended when it has one).
        end: Window end.

    Returns:
        str: `<facet-slug>[_v<version>]_<startYYYYMMDD>_<endYYYYMMDD>` (the
            version tag is added when the store carries one; dates are omitted
            when unavailable).
    """
    stem = f"{store.slug}_v{store.version}" if store.version else store.slug
    tokens = [token for token in (_date_token(start), _date_token(end)) if token]
    fragment = "_".join(tokens)
    return f"{stem}_{fragment}" if fragment else stem


def _date_token(value: Any) -> str:
    """Return `value` formatted as `%Y%m%d`, or `""` when it has no date.

    Args:
        value: A `datetime`-like value or `None`.

    Returns:
        str: The `%Y%m%d` token, or `""`.
    """
    try:
        return cast("str", value.strftime("%Y%m%d"))
    except AttributeError:
        return ""
