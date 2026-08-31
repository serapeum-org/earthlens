"""ECMWF / Copernicus data-store backend — :class:`ECMWF`, an :class:`AbstractDataSource`.

Downloads from the five CADS data stores through one `cdsapi` client:
the Copernicus trio (CDS, ADS, EWDS) and the two ECMWF-hosted stores
(ECDS, XDS). A request is `{dataset: [variable, ...], ...}` plus a date
range, a bbox and a temporal resolution; the dataset ids, variable
metadata and per-store routing all come from
:class:`earthlens.ecmwf.Catalog` (loaded from the per-family
`catalog/*.yaml` shards). Nothing about a dataset is hardcoded here.

Each row's `endpoint` picks the store, resolved to an API root and a
credential by :mod:`earthlens.ecmwf.endpoints`; one client is cached per
endpoint, so a single :meth:`ECMWF.download` may span several stores.

The pipeline (per `(dataset, variable)` pair) is:

1. :meth:`ECMWF._build_request` — build the request from the catalog row,
   then shape its date keys by `request_kind` (see
   :data:`_REQUEST_KIND_STRIPS`): kinds strip the template fields their
   dataset rejects, and the reforecast kinds rewrite the date axes —
   `glofas_hindcast` *renames* `year`/`month`/`day` to the `h*` keys,
   while `s2s_reforecast` *copies* them, keeping both dates.
2. :class:`earthlens.ecmwf.constraints.RequestValidator` — pre-flight the
   request against the store's `constraints.json` before anything is queued.
3. :meth:`ECMWF._api` — submit through `cdsapi`, then normalise the
   response: a zipped or archived NetCDF is unpacked
   (:func:`_unpack_netcdf_archive`) so `download()` always returns the
   written data files.

Authentication failures are surfaced as :class:`AuthenticationError`,
which distinguishes missing credentials from an unaccepted licence — the
two most common ways a retrieve is refused.
"""

from __future__ import annotations

import math
import os
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from functools import partial
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from earthlens.aggregate import AggregationConfig, aggregate_netcdf
from earthlens.base import (
    AbstractDataSource,
    LazyClientMixin,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
    date_windows,
    to_datetime,
)
from earthlens.base import AuthenticationError as _BaseAuthenticationError
from earthlens.config import resolve_output_path
from earthlens.ecmwf._helpers import (
    CadsUnavailableError,
    _retrieve_with_retry,
    endpoint_for,
)
from earthlens.ecmwf.catalog import Catalog, Variable
from earthlens.ecmwf.constraints import RequestValidator
from earthlens.ecmwf.endpoints import constraints_base_url
from earthlens.ecmwf.endpoints import open_client as _open_endpoint_client

__all__ = ["AuthenticationError", "ECMWF", "ERA5_GRID_DEGREES"]


ERA5_GRID_DEGREES: float = 0.125

# Per-request-kind keys to drop from the request dict before the
# retrieve call. The keys here name the *template defaults* (built
# unconditionally by :meth:`ECMWF._api`) that are invalid for the
# named request kind. Per-row `extras` are still merged on top, so
# users can supply alternative values for any stripped key.
# `product_type` is catalog-driven (see `Variable.product_type`) and
# no longer appears in any strip list.
_REQUEST_KIND_STRIPS: dict[str, tuple[str, ...]] = {
    "form": (),
    # ORAS5 (and any monthly ocean dataset that mirrors NEMO's
    # request shape): no `day` / `time` selectors, no `area`
    # bbox cropping.
    "oceanic_monthly": ("day", "time", "area"),
    # CARRA-means and similar aggregate datasets: drop `time`
    # because the aggregate is over the window indicated by
    # `time_aggregation`.
    "carra_means": ("time",),
    # GloFAS (EWDS): the forecast horizon is selected by `leadtime_hour`
    # (carried in `extras`), not a time-of-day; the dataset rejects the
    # four 6-hourly `time` slots the daily template adds, so drop `time`.
    "glofas": ("time",),
    # GloFAS/EFAS hindcast (reforecast): keys on `hyear`/`hmonth`/`hday`
    # (remapped in `_build_request`) + `leadtime_hour`; drop `time`.
    "glofas_hindcast": ("time",),
    # S2S reforecast (ECDS): unlike `glofas_hindcast` this keeps BOTH date
    # axes — `year`/`month`/`day` select the model cycle and
    # `hyear`/`hmonth`/`hday` the reforecast — so the month/day are *copied*
    # into the h-keys rather than renamed. `hyear` alone comes from `extras`.
    # The strip tuple is deliberately empty: the form accepts every template
    # key including `time`, so there is nothing to drop (the row's `extras`
    # pin the single `time` slot the dataset serves).
    "s2s_reforecast": (),
    # Seasonal (GloFAS/EFAS/CDS seasonal): keyed by `year`/`month` + a lead
    # (`leadtime_month`/`leadtime_hour`) + `originating_centre`/`system` from
    # `extras`; no `day`, no time-of-day.
    "seasonal": ("day", "time"),
    # Seasonal hindcast (EFAS seasonal-reforecast): `hyear`/`hmonth` (remapped)
    # + `leadtime_hour`; no `hday`, no time-of-day.
    "seasonal_hindcast": ("day", "time"),
    # CAMS grid datasets (ADS): a single `date` range string replaces
    # year/month/day and `product_type` (see the `cams_date` branch); nothing
    # extra to strip here.
    "cams_date": (),
    # CEMS fire danger (EWDS): daily year/month/day + a `grid` selector, no
    # time-of-day; drop the template's `time` slots.
    "fire": ("time",),
    # CAMS year/month datasets (ADS): GHG inversion + European air-quality
    # reanalyses key on year/month (no day), no time-of-day, and reject the
    # `area` bbox (global-gridded); `data_format`/`product_type` are dropped
    # per-row via `extras: {…: null}`.
    "cams_inversion": ("day", "time", "area"),
    # Satellite Climate Data Records (CDS): year/month/day + per-CDR selectors
    # (sensor/version/…) from `extras`, no time-of-day; the response is a
    # zip-of-NetCDF (unpacked by the C3 handler) with no `data_format` choice
    # (dropped per-row via `extras: {data_format: null}`).
    "satellite_cdr": ("time",),
}


class AuthenticationError(_BaseAuthenticationError):
    """Raised when cdsapi cannot authenticate against the Climate Data Store.

    The ECMWF backend uses :class:`cdsapi.Client` to talk to CDS. The
    client reads its credentials from `~/.cdsapirc` (or the
    `CDSAPI_URL` / `CDSAPI_KEY` environment variables). If the
    config is missing or malformed, :meth:`ECMWF._initialize` wraps the
    underlying error in this exception so callers can distinguish auth
    problems from generic CDS server errors.

    See Also:
        https://cds.climate.copernicus.eu/how-to-api: Official cdsapi
            setup guide, including PAT generation and the
            `~/.cdsapirc` format.
    """

    pass


def _looks_like_missing_credentials(exc: BaseException) -> bool:
    """Heuristic: does this exception come from missing CDS credentials?

    cdsapi does not expose typed exception classes for auth failures —
    they surface as generic `Exception` with messages like "Missing/
    incomplete configuration file" or "key not found". We classify by
    presence of the dotfile and env vars first (no dotfile + no env
    vars → almost certainly missing credentials), then fall back to a
    keyword scan of the exception message.

    Args:
        exc: The exception raised by `cdsapi.Client()`.

    Returns:
        True when the failure looks like a credential / config-file
        problem (so it is safe to wrap as :class:`AuthenticationError`),
        False for transport / network / library errors that should
        propagate untouched.
    """
    cdsapirc_present = (Path.home() / ".cdsapirc").is_file()
    env_present = bool(os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"))
    auth_keywords = (
        "configuration",
        "credentials",
        "cdsapirc",
        "key not found",
        "missing url",
        "missing key",
    )
    message = str(exc).lower()
    no_credentials = not cdsapirc_present and not env_present
    message_indicates_auth = any(keyword in message for keyword in auth_keywords)
    return no_credentials or message_indicates_auth


def _unwrap_zipped_netcdf(target: Path) -> None:
    r"""Replace `target` with its inner NetCDF when CDS returned a zip.

    CDS occasionally hands back a zip-wrapped NetCDF even when
    `data_format='netcdf'` was requested (observed on
    `reanalysis-era5-land-monthly-means` and similar partitioned
    datasets). The `cdsapi.Client.retrieve` call writes the raw bytes
    to `target` regardless of format, so the file ends up with a
    `.nc` name but a `PK\x03\x04` zip header. Detect that and
    extract the single inner NetCDF in place so downstream callers
    (the aggregator, user code reading the file) see a real NetCDF.

    Streams the inner member to a sibling temp file via
    `shutil.copyfileobj` (default 64 KiB buffer) and then atomically
    swaps it onto `target` via `os.replace`. The inner NetCDF is
    never fully materialised in Python memory regardless of size.
    The temp file is cleaned up on every error path.

    No-op when `target` is already a plain NetCDF, or when the zip
    does not contain exactly one `.nc` member (other shapes are
    surfaced via a `RuntimeError` so they do not silently pass).
    """
    if not zipfile.is_zipfile(target):
        return
    tmp = target.parent / (target.name + ".unwrap.tmp")
    try:
        with zipfile.ZipFile(target) as zf:
            members = [m for m in zf.namelist() if m.endswith(".nc")]
            if len(members) != 1:
                raise RuntimeError(
                    f"CDS returned a zip with {len(members)} .nc members at "
                    f"{target}; expected exactly one. Members: {zf.namelist()}"
                )
            inner = members[0]
            with zf.open(inner) as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        bytes_written = tmp.stat().st_size
        os.replace(tmp, target)
        logger.debug(
            f"Unwrapped CDS zip response at {target}: extracted inner "
            f"{inner!r} ({bytes_written} bytes)"
        )
    finally:
        # On the success path os.replace consumed `tmp`, so this is a
        # no-op. On any failure path (RuntimeError before extraction,
        # I/O error during copy, os.replace failure) the partially
        # written temp file is removed so we never leave litter.
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _detect_output_format(target: Path) -> str:
    """Classify a retrieved file by its magic bytes.

    The store's `data_format` is a request hint, not a guarantee (CDS zips a
    NetCDF even when `netcdf` is asked for). Sniffing the leading bytes is the
    reliable signal for the format handler.

    Args:
        target: The file a retrieve wrote.

    Returns:
        str: `"zip"`, `"netcdf"`, `"grib"`, or `"unknown"`.
    """
    try:
        with target.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return "unknown"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:4] == b"GRIB":
        return "grib"
    if head[:3] == b"CDF" or head[:4] == b"\x89HDF":
        return "netcdf"
    return "unknown"


def _unique_dest(out_dir: Path, basename: str, taken: list[Path]) -> Path:
    """Return a collision-free destination under `out_dir` for a member basename.

    Two zip members flattened to the same basename (from different in-zip
    subdirectories) would overwrite each other; suffix `_1`, `_2`, … on collision
    and log, so no member is silently lost.

    Args:
        out_dir: The directory members are extracted into.
        basename: The member's flattened (basename-only) filename.
        taken: The destinations already used in this extraction.

    Returns:
        Path: A path under `out_dir` present in neither `taken` nor on disk.
    """
    dest = out_dir / basename
    if dest not in taken and not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while (candidate := out_dir / f"{stem}_{n}{suffix}") in taken or candidate.exists():
        n += 1
    logger.warning(
        f"zip member basename {basename!r} collided in {out_dir.name}/; wrote "
        f"{candidate.name} instead of overwriting an already-extracted member"
    )
    return candidate


def _unpack_netcdf_archive(target: Path) -> list[Path]:
    r"""Unpack a zip-of-NetCDF response into its member NetCDFs.

    Many `satellite-*` CDRs and the ADS `netcdf_zip` format return a zip whose
    members are per-timestep / per-variable NetCDFs. A single-member zip is
    unwrapped in place (the file keeps its name); a multi-member zip is
    extracted into a sibling `<stem>/` directory and the original archive is
    removed, so downstream code sees real NetCDFs, not a blob. Member names are
    flattened to their basename to defeat zip path-traversal. Non-zip inputs
    and zips with no `.nc` member are returned unchanged.

    Args:
        target: The file a retrieve wrote.

    Returns:
        list[Path]: The resulting NetCDF path(s) — `[target]` for a plain file
        or single-member zip, or the extracted members for a multi-member zip.
    """
    if not zipfile.is_zipfile(target):
        return [target]
    with zipfile.ZipFile(target) as archive:
        all_names = archive.namelist()
    members = [name for name in all_names if name.endswith(".nc")]
    if not members:
        logger.warning(f"{target.name}: zip has no .nc member; left raw {all_names}")
        return [target]
    if len(members) == 1:
        # Single-member — unwrap in place (the archive is already closed, so the
        # atomic swap won't hit a Windows file lock).
        _unwrap_zipped_netcdf(target)
        return [target]
    out_dir = target.parent / target.stem
    # Start clean: a prior multi-member retrieve to the same path left its members
    # here, and `_unique_dest` would otherwise treat them as collisions and suffix
    # this retrieve's members (`_1`/`_2`), so the caller's `glob("*.nc")` would
    # return a stale + fresh (possibly foreign-window) union. Reset the dir so it
    # holds exactly this retrieve's members.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(target) as archive:
        for name in members:
            # Flatten to the basename to defeat path traversal, then de-collide:
            # two members from different in-zip subdirs (`2020/d.nc`, `2021/d.nc`)
            # share a basename, and writing both to it would silently drop one.
            dest = _unique_dest(out_dir, Path(name).name, extracted)
            with archive.open(name) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(dest)
    target.unlink(missing_ok=True)
    logger.debug(
        f"Unpacked {len(extracted)} NetCDF member(s) from {target.name} → {out_dir}"
    )
    return sorted(extracted)


def _describe_pair(pair: tuple[str, str]) -> str:
    """Render a `(dataset, variable)` pair for the `_run_items` log lines.

    Args:
        pair: The `(dataset name, variable code)` that failed.

    Returns:
        str: `"<dataset>/<variable>"`.
    """
    return f"{pair[0]}/{pair[1]}"


def _remap_date_keys(
    request: dict[str, Any], pairs: tuple[tuple[str, str], ...]
) -> None:
    """Rename request date keys in place (e.g. `year`→`hyear` for hindcasts).

    Args:
        request: The request dict being assembled (mutated in place).
        pairs: `(source_key, destination_key)` renames to apply when the
            source key is present.
    """
    for src_key, dst_key in pairs:
        if src_key in request:
            request[dst_key] = request.pop(src_key)


def _reject_multi_day_reforecast(request: dict[str, Any], var_info: Variable) -> None:
    """Refuse an S2S-reforecast window that spans more than one model-cycle day.

    The model-cycle and reforecast dates are paired, but a CDS form request
    treats every list as an independent cross-product axis, so a window of `n`
    days would submit `n x n` `day`/`hday` combinations of which only the `n`
    diagonal pairs exist. There is no request shape that expresses the pairing,
    so ask for one day at a time.

    Args:
        request: The request assembled so far.
        var_info: The catalog row being requested (named in the error).

    Raises:
        ValueError: If the window covers more than one day.
    """
    days = request.get("day") or []
    months = request.get("month") or []
    if not days:
        raise ValueError(
            f"{var_info.cds_dataset!r} selects a reforecast by the model run's "
            "own calendar day, so it needs a `day`. Request it with "
            "temporal_resolution='daily'."
        )
    if len(days) > 1 or len(months) > 1:
        raise ValueError(
            f"{var_info.cds_dataset!r} pairs the model-cycle date with the "
            "reforecast date, and a CDS form request cannot express that "
            f"pairing: a {len(days)}-day window would submit "
            f"{len(days) * len(days)} day/hday combinations of which only "
            f"{len(days)} exist. Request one model-cycle date at a time "
            "(start == end)."
        )
    # A 29 February model cycle has no reforecast in a non-leap `hyear`. The
    # row's `extras` are merged after this hook runs, so read `hyear` from the
    # catalog row rather than from the half-built request.
    hyears = var_info.extras.get("hyear") or []
    if months == ["02"] and days == ["29"]:
        non_leap = [
            year
            for year in hyears
            if not (int(year) % 4 == 0 and (int(year) % 100 or int(year) % 400 == 0))
        ]
        if non_leap:
            raise ValueError(
                f"{var_info.cds_dataset!r}: a 29 February model cycle has no "
                f"reforecast in the non-leap hyear(s) {non_leap}. Pick a leap "
                "`hyear` in the row's extras, or another model-cycle date."
            )


def _apply_request_kind_dates(
    request: dict[str, Any], var_info: Variable, start_date: Any, end_date: Any
) -> None:
    """Rewrite the request's date keys for the date-representation kinds (G11).

    `cams_date` replaces year/month/day with a single `date` range string;
    `glofas_hindcast` / `seasonal_hindcast` *rename* year/month(/day) to the
    `hyear`/`hmonth`(/`hday`) hindcast-reference keys; `s2s_reforecast`
    *copies* month/day into them instead, because that dataset needs both the
    model-cycle and the reforecast date (and rejects a window it cannot
    express). Any other kind is a no-op.

    Args:
        request: The request dict assembled so far (mutated in place).
        var_info: The catalog row whose `request_kind` selects the rewrite.
        start_date: The window start (used for the `cams_date` range).
        end_date: The window end (used for the `cams_date` range).
    """
    if var_info.request_kind == "cams_date":
        for key in ("year", "month", "day", "product_type"):
            request.pop(key, None)
        request["date"] = f"{start_date:%Y-%m-%d}/{end_date:%Y-%m-%d}"
        # Reset the daily template's 6-hourly slots to a single default
        # (a per-row `extras: {time: [...]}` overrides it later).
        request["time"] = ["00:00"]
    elif var_info.request_kind == "glofas_hindcast":
        _remap_date_keys(
            request, (("year", "hyear"), ("month", "hmonth"), ("day", "hday"))
        )
    elif var_info.request_kind == "seasonal_hindcast":
        # Seasonal reforecast: hindcast year/month, no day (stripped elsewhere).
        _remap_date_keys(request, (("year", "hyear"), ("month", "hmonth")))
    elif var_info.request_kind == "s2s_reforecast":
        # S2S reforecasts carry two coupled date axes: the model cycle
        # (`year`/`month`/`day`) and the reforecast (`hyear`/`hmonth`/`hday`).
        # The store only serves a reforecast on the model run's own calendar
        # day, so the two must be *paired*, not crossed — and a CDS form
        # request cannot express "zip these two lists": every list is a
        # cross-product axis. A multi-day window would therefore submit
        # `day x hday` combinations of which only the diagonal exists, so
        # refuse it explicitly rather than send a request the store rejects
        # (or, worse, one it partially serves).
        _reject_multi_day_reforecast(request, var_info)
        for src_key, dst_key in (("month", "hmonth"), ("day", "hday")):
            if src_key in request:
                # Copy by value: assigning the list itself would alias the two
                # keys, so a later edit to `day` would silently move `hday`.
                request[dst_key] = list(request[src_key])


def _apply_extras_and_strips(request: dict[str, Any], var_info: Variable) -> None:
    """Merge per-row extras, drop the request-kind strips, apply None opt-outs.

    The strip runs after the extras merge so a user can re-introduce a stripped
    key by setting it in `extras`; the `None` opt-out then drops any `extras`
    key explicitly set to `None`.

    A list arriving from `extras` is copied rather than assigned. The catalog
    row is cached for the life of the process, so sharing it would let an edit
    to one request rewrite that variable for every later retrieve.

    Args:
        request: The request dict assembled so far (mutated in place).
        var_info: The catalog row supplying `extras` and `request_kind`.
    """
    # Copy by value: the catalog row is cached for the life of the process, so
    # assigning a list or dict straight out of `extras` would let an edit to one
    # request rewrite it for every later retrieve of that variable. The levels
    # this PR promotes to first-class arrive exactly this way.
    request.update(
        {
            key: list(value) if isinstance(value, list) else value
            for key, value in var_info.extras.items()
        }
    )
    for stripped in _REQUEST_KIND_STRIPS.get(var_info.request_kind, ()):
        if stripped not in var_info.extras:
            request.pop(stripped, None)
    for key, value in list(var_info.extras.items()):
        if value is None:
            request.pop(key, None)


def _render_level(level: Any) -> str:
    """Render one pressure level the way CDS spells it.

    A whole number written as a float — `500.0`, which is what arithmetic on
    levels produces — would otherwise reach the store as `"500.0"` and match no
    level it offers. Any real number is accepted, so a numpy scalar out of an
    array of levels renders like the builtin it stands for.

    A level is a pressure in hPa, so anything that does not read as a finite
    number is refused here rather than sent, and one written with surrounding
    space or in exponent form is rendered from the number it parses to rather
    than echoed back. The store's own constraint check
    would catch most of them, but not under `skip_constraints=True` and not
    offline, and a rejected request says far less than this does.

    Args:
        level: A single level.

    Returns:
        The level as a string.

    Raises:
        TypeError: If given a bool, which is a `numbers.Real` and would
            otherwise render as `"True"`.
        ValueError: If the level does not read as a number, or reads as one
            that is not finite.
    """
    if isinstance(level, bool):
        raise TypeError(f"pressure_level= takes a number, not {level!r}.")
    if isinstance(level, Real) and not isinstance(level, str):
        value = float(level)
        if not math.isfinite(value):
            raise ValueError(f"{level!r} is not a finite pressure level.")
        return str(int(value)) if value.is_integer() else str(level)
    text = str(level).strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(
            f"{level!r} is not a pressure level; levels are numbers in hPa."
        ) from None
    if not math.isfinite(value):
        raise ValueError(f"{level!r} is not a finite pressure level.")
    # Rendered from the parsed number rather than echoed, so a level spelled
    # `" 500 "` or `"1e3"` reaches the store as `"500"` and `"1000"` — both
    # parse, and neither matches a level as written.
    return str(int(value)) if value.is_integer() else text


def _normalize_pressure_level(
    pressure_level: Any | None,
) -> list[str] | None:
    """Normalize the `pressure_level=` override to a list of strings.

    CDS spells a level as a string, but hPa reads as a number, so `500` and
    `[500]` are both natural things to write. Each level is rendered by
    :func:`_render_level` from the number it parses to rather than echoed, and
    a lone level is wrapped so the single-level case needs no brackets. Any
    ordered iterable of levels is accepted, a numpy array included.

    Args:
        pressure_level: Levels to request, a single level, or None.

    Returns:
        The levels as a list of strings, or None to keep each row's own level.

    Raises:
        TypeError: If given something that is neither a level nor a sequence
            of levels. A mapping, a set and a `bytes` are refused by name
            rather than iterated — they yield keys, an unspecified order, and
            byte values respectively — and a bool is refused by
            :func:`_render_level` as a `numbers.Real`.
        ValueError: If given an empty sequence, or a level that does not read
            as a number. `pressure_level: []` is not a valid request and asking
            for no levels is not what any caller means; `None` is how a caller
            declines to override.
    """
    if pressure_level is None:
        return None
    if isinstance(pressure_level, (str, Real)):
        return [_render_level(pressure_level)]
    if isinstance(
        pressure_level, (Mapping, AbstractSet, bytes, bytearray)
    ) or not isinstance(pressure_level, Iterable):
        raise TypeError(
            "pressure_level= takes a level or a sequence of levels, not "
            f"{type(pressure_level).__name__}."
        )
    levels = [_render_level(level) for level in pressure_level]
    if not levels:
        raise ValueError(
            "pressure_level= was given no levels; pass None to keep each "
            "catalog row's own level, or name at least one level to request."
        )
    return levels


class ECMWF(LazyClientMixin, AbstractDataSource):
    """ECMWF / Copernicus Climate Data Store backend.

    Downloads ERA5 reanalysis (and ERA5-Land where the catalog
    indicates) via :class:`cdsapi.Client`. The user-friendly variable
    short codes (e.g. `"2m-temperature"`, `"total-precipitation"`) are resolved through
    :class:`Catalog`, which loads the per-variable metadata from the
    bundled CDS catalog (the `catalog/` directory).

    The download pipeline (per variable) is a single step:

    * :meth:`_api` — build the cdsapi request dict (daily / monthly
      branch on `temporal_resolution`) and submit it via
      `client.retrieve(dataset, request, target)`. Returns the
      absolute path to the NetCDF that CDS wrote.

    Per-date GeoTIFF post-processing (time-window mean, flux
    scaling, raster output) is intentionally not part of the
    package — see `examples/post_process_ecmwf_netcdf.py` for a
    runnable script that consumes the NetCDF this method writes.

    The valid `temporal_resolution` values are `"daily"` and
    `"monthly"`. `_check_input_dates` raises `ValueError` for
    anything else; that is the authoritative gate. Spatial cell
    size lives on :attr:`SpatialExtent.resolution` (populated by
    :meth:`_create_grid`) and is the request's native grid spacing —
    :data:`ERA5_GRID_DEGREES` (0.125°) for regular CDS datasets, or a
    dataset's own `grid_resolution` (e.g. GloFAS's 0.05° on EWDS).
    """

    OUTPUT_KIND: OutputKind = "raster"

    #: Wires the temporal reducer (ARC-1).
    SUPPORTS_AGGREGATE = True

    #: Clips to the exact polygon when `aoi=` carries one, not just its bbox.
    SUPPORTS_POLYGON_AOI = True

    #: Retrieval-level pressure override, `None` unless a caller sets
    #: `pressure_level=`. Declared on the class because `_build_request` reads
    #: it for every request, while a cheap instance built with
    #: `ECMWF.__new__` - the idiom this module's own docstrings advertise -
    #: never runs `__init__`.
    pressure_level: list[str] | None = None

    def __init__(
        self,
        start: str | None = None,
        end: str | None = None,
        variables: dict[str, list[str]] | None = None,
        lat_lim: list[float] | None = None,
        lon_lim: list[float] | None = None,
        temporal_resolution: str = "daily",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        skip_constraints: bool = False,
        request: dict[str, Any] | None = None,
        endpoint: str | None = None,
        pressure_level: Any | None = None,
    ):
        """Initialize an ECMWF backend instance.

        Forwards every argument to :class:`AbstractDataSource`, which
        captures the bbox/date dict into `self.space` / `self.time`. The
        cdsapi client is built lazily on first access to `self.client`
        (via :meth:`_open_client`), so construction never authenticates.

        Args:
            start: Inclusive start date as a string (parsed with
                `fmt`). Required.
            end: Inclusive end date as a string. Required.
            variables: Mapping from CDS dataset short name to a list
                of variable codes drawn from that dataset, e.g.
                `{"reanalysis-era5-single-levels": ["2m-temperature",
                "total-precipitation"]}`. The dataset name must be a
                key of :attr:`Catalog.datasets`; each variable name
                must appear under that dataset's `variables:` block.
                See the bundled CDS catalog (`catalog/`) for the keys.
                Required.
            lat_lim: `[lat_min, lat_max]`. Required.
            lon_lim: `[lon_min, lon_max]`. Required.
            temporal_resolution: Either `"daily"` or `"monthly"`.
                Defaults to `"daily"`.
            path: Output directory. Created by the parent if it does
                not exist. When omitted it falls back to the
                configured earthlens output directory (`set_output_dir()` /
                `EARTHLENS_DATA_DIR`); see `earthlens.config`.
            fmt: `strptime` format for `start` / `end`.
                Defaults to `"%Y-%m-%d"`.
            skip_constraints: When `True`, every CDS pre-flight
                validation phase (date / area sanity, variable typo
                check, required-fields check, combinatorial cover
                check) is bypassed and the request is sent to CDS
                unchecked. Useful when CDS's published
                `constraints.json` is stale or wrong for the
                dataset, or when running offline. Defaults to `False`.
            pressure_level: Pressure levels in hPa to retrieve, replacing
                the level each catalog row carries. A lone level needs no
                brackets and may be written as a number, so `500`, `"500"`
                and `[500]` are the same request.

                Replaces the `pressure_level` of any request that already
                has one, whether the catalog spelled it as
                `cds_pressure_level` or in the row's `extras` — the CARRA
                means family does the latter. A request without that key
                keeps none: a single-level row would be made invalid rather
                than broader by acquiring one, and a model-level row is
                selected by `model_level`, not by this. Such a row is logged
                rather than silently skipped.

                Defaults to `None`, which keeps each row's own level.

        Raises:
            ValueError: If `pressure_level=` is combined with `request=`. The
                raw request is forwarded verbatim, so the override would be
                accepted and never consulted.
        """
        self.skip_constraints = skip_constraints
        # Per-endpoint cdsapi client cache (one per ENDPOINTS slug). Populated
        # lazily by `_client_for` so a multi-endpoint download reuses one
        # connection per CADS instance. `_injected_client` holds a client
        # bound via the `client` setter (used for every endpoint); it stays
        # `None` for the normal lazy path so reading `self.client` cannot
        # poison endpoint routing.
        self._clients: dict[str, Any] = {}
        self._injected_client: Any = None
        # Retrieval-time override for the catalog's pressure level. The curated
        # rows carry the single level each was audited at, so without this a
        # different level means editing the shipped YAML or building a
        # `Variable` by hand and bypassing the facade.
        self.pressure_level: list[str] | None = _normalize_pressure_level(
            pressure_level
        )
        # Raw-request passthrough (the coverage lever): when `request=` is
        # given, skip the typed catalog / date / grid machinery and forward
        # the raw request to the resolved store's client (see `download`). The
        # dataset id arrives as the single key of `variables` — the facade
        # composes `dataset=<id>` into `variables={<id>: []}`, so a passthrough
        # is `EarthLens('ecmwf', dataset=<id>, request=<dict>)`.
        self._passthrough: dict[str, Any] | None = None
        if request is not None and self.pressure_level is not None:
            # Any `request=` takes the passthrough, an empty one included, and
            # the passthrough forwards what it is given verbatim - so an
            # override would be accepted and then never consulted.
            raise ValueError(
                "pressure_level= does not apply when request= is given: the "
                "raw request is forwarded as-is, so put the level in it."
            )
        if request is not None:
            dataset = (
                next(iter(variables))
                if isinstance(variables, dict) and len(variables) == 1
                else None
            )
            if not dataset:
                raise ValueError(
                    "ECMWF raw passthrough needs the dataset id alongside "
                    "`request=<dict>` — pass `dataset=<id>` (facade) or "
                    "`variables={<id>: []}`."
                )
            self._passthrough = {
                "dataset": str(dataset),
                "request": dict(request),
                "endpoint": endpoint,
            }
            # Construction stays read-only: the base `download` wrapper's
            # `_ensure_root_dir` creates (and unwinds on failure) the output
            # directory at download time, exactly as the typed path relies on.
            self.root_dir = resolve_output_path(path)
            self.path = self.root_dir
            return

        missing = [
            name
            for name, value in (
                ("start", start),
                ("end", end),
                ("variables", variables),
                ("lat_lim", lat_lim),
                ("lon_lim", lon_lim),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"ECMWF requires {missing} — or pass `dataset=`+`request=` "
                "for a raw-request passthrough."
            )
        # The `missing` guard above already rejects a None in any of these, but
        # mypy cannot narrow through the comprehension — assert so the typed
        # `super().__init__` call sees the non-optional types (B101 is skipped).
        assert start is not None and end is not None and variables is not None
        assert lat_lim is not None and lon_lim is not None
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

    def _check_input_dates(
        self, start: str, end: str, temporal_resolution: str, fmt: str
    ) -> TemporalExtent:
        """Parse the date range and produce the iteration index.

        Returned dict is captured by
        :meth:`AbstractDataSource.__init__` into `self.time` so
        :meth:`_api` can access the parsed bounds and the per-date
        pandas range without re-parsing.

        Args:
            start: Inclusive start date as a string.
            end: Inclusive end date as a string.
            temporal_resolution: `"daily"` (uses `freq="D"`) or
                `"monthly"` (uses `freq="MS"`).
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen pydantic model with `start_date`,
            `end_date`, `resolution` (pandas frequency alias —
            `"D"` for daily, `"MS"` for month-start), and
            `dates` (the :class:`pandas.DatetimeIndex` the
            download loop iterates).

        Raises:
            ValueError: If `temporal_resolution` is neither
                `"daily"` nor `"monthly"`, or if the parsed
                `start` is later than the parsed `end`.
        """
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)

        if temporal_resolution == "daily":
            dates = date_windows(start_dt, end_dt, "D")
            resolution = "D"
        elif temporal_resolution == "monthly":
            dates = date_windows(start_dt, end_dt, "MS")
            resolution = "MS"
        else:
            raise ValueError(
                "temporal_resolution should be either 'daily' or 'monthly'"
            )

        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=resolution,
            dates=dates,
        )

    def _open_client(self, endpoint: str = "cds"):
        """Construct a :class:`cdsapi.Client` for the named CADS endpoint.

        Delegates to :func:`earthlens.ecmwf.endpoints.open_client`, which maps
        the endpoint slug (`"cds"` / `"ads"` / `"ewds"`) to its URL and resolves
        the token (the endpoint's own `<ENDPOINT>_KEY`, else the shared CDS
        Personal Access Token from `CDSAPI_KEY` / `~/.cdsapirc`). Missing-CDS-
        credential errors are re-raised as :class:`AuthenticationError` with a
        message telling the user where to put their token. Called via
        :meth:`_client_for` (and, for the default endpoint, by the
        :class:`~earthlens.base.LazyClientMixin` `client` property).

        Args:
            endpoint: CADS instance slug. Defaults to `"cds"`.

        Returns:
            cdsapi.Client: Authenticated client for `endpoint`. Calls to
            `client.retrieve(...)` use this connection.

        Raises:
            AuthenticationError: If cdsapi cannot authenticate — typically
                because `~/.cdsapirc` is missing, malformed, or contains an
                old-API-style `email` line.

        Examples:
            - Construct a client when credentials are properly
              configured. Marked `# doctest: +SKIP` because it
              requires a real `~/.cdsapirc`:

                ```python
                >>> ecmwf = ECMWF(  # doctest: +SKIP
                ...     start="2022-01-01",
                ...     end="2022-01-01",
                ...     variables={
                ...         "reanalysis-era5-single-levels": ["2m-temperature"],
                ...     },
                ...     lat_lim=[4.0, 5.0],
                ...     lon_lim=[-75.0, -74.0],
                ...     path="examples/data/era5",
                ... )
                >>> ecmwf.client  # doctest: +SKIP

                ```
        """
        try:
            client = _open_endpoint_client(endpoint)
        except Exception as exc:  # noqa: BLE001 - cdsapi raises a variety of types; classify here and re-raise as AuthenticationError
            if isinstance(exc, AuthenticationError):
                raise
            if _looks_like_missing_credentials(exc):
                raise AuthenticationError(
                    "cdsapi could not authenticate against the Climate "
                    "Data Store. Create ~/.cdsapirc (Windows: "
                    "C:\\Users\\<USER>\\.cdsapirc) with:\n"
                    "    url: https://cds.climate.copernicus.eu/api\n"
                    "    key: <YOUR-PERSONAL-ACCESS-TOKEN>\n"
                    "Generate a Personal Access Token at "
                    "https://cds.climate.copernicus.eu/profile and "
                    "accept the licence for each dataset you intend to "
                    "download. See https://cds.climate.copernicus.eu/how-to-api for "
                    "the full setup guide."
                ) from exc
            raise

        return client

    @property
    def client(self) -> Any:
        """The default (CDS) cdsapi client — opened lazily and cached per endpoint.

        Overrides :class:`~earthlens.base.LazyClientMixin` so that reading
        `self.client` (e.g. via `authenticate()`) routes through the same
        per-endpoint cache as a retrieve, rather than seeding a shared slot that
        would then be returned for every endpoint. Resolves the `"cds"` client;
        `authenticate()` therefore warms the CDS endpoint, while a non-CDS
        endpoint (e.g. EWDS for GloFAS) is built lazily on its first retrieve.

        Returns:
            cdsapi.Client: The CDS client (built on first use, then cached).
        """
        return self._client_for("cds")

    @client.setter
    def client(self, value) -> None:
        """Bind a client used for every endpoint (a deliberate override).

        A client set here overrides endpoint routing and is returned by
        :meth:`_client_for` for all endpoints — unlike a lazily-built endpoint
        client, which is cached per endpoint and never treated as injected.

        Args:
            value: The client object to use for every endpoint.
        """
        self._injected_client = value

    def _client_for(self, endpoint: str):
        """Return the cdsapi client for `endpoint`, honouring an injected one.

        An **explicitly bound** client (set via the `client` setter) is returned
        for every endpoint. Otherwise a client is built once per endpoint and
        cached on `self._clients` so repeated retrieves against the same CADS
        instance reuse the connection. A lazily-built endpoint client is never
        mistaken for a bound one, so reading `self.client` (which resolves
        `"cds"`) cannot poison routing to another endpoint.

        Args:
            endpoint: CADS instance slug (`"cds"` / `"ads"` / `"ewds"`).

        Returns:
            cdsapi.Client: The client to use for a retrieve against `endpoint`.
        """
        injected = getattr(self, "_injected_client", None)
        if injected is not None:
            return injected
        if endpoint not in self._clients:
            self._clients[endpoint] = self._open_client(endpoint)
        return self._clients[endpoint]

    def _grid_resolution_for_request(self) -> float:
        """Resolve the grid spacing to snap the bbox to for this request.

        Reads the requested datasets from `self.vars` and returns the finest
        (smallest) `grid_resolution` declared among them in the catalog,
        falling back to :data:`ERA5_GRID_DEGREES` for any dataset that declares
        none. When `self.vars` is absent (a bare instance with no requested
        datasets) or the catalog lookup fails, the ERA5 default is used — so
        regular CDS datasets keep their historic 0.125° snap while an EWDS
        dataset like GloFAS snaps to its native 0.05°.

        Mixing datasets of differing native resolution in one request is
        best-effort: the single instance-level bbox is snapped to the finest
        grid, so a coarser dataset's `area` may not sit exactly on its own cell
        edges (the server re-snaps to the delivered grid regardless). Split
        datasets of differing native resolution into separate calls if exact
        per-dataset bbox alignment matters.

        Returns:
            float: The grid spacing in degrees to snap the bbox to.
        """
        variables = getattr(self, "vars", None)
        if not variables:
            return ERA5_GRID_DEGREES
        try:
            catalog = Catalog()
        except Exception:  # noqa: BLE001 - a bad catalog must not break grid snapping
            return ERA5_GRID_DEGREES
        resolutions: list[float] = []
        for dataset_name in variables:
            dataset = catalog.datasets.get(dataset_name)
            # Each dataset contributes its own native spacing; a dataset that
            # declares none (or is unknown) falls back to the ERA5 default. The
            # ERA5 default is a per-dataset fallback, not a global floor, so a
            # dataset coarser than 0.125° keeps its native grid.
            if dataset is not None and dataset.grid_resolution is not None:
                resolutions.append(dataset.grid_resolution)
            else:
                resolutions.append(ERA5_GRID_DEGREES)
        return min(resolutions) if resolutions else ERA5_GRID_DEGREES

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Snap a lat/lon bounding box to the request's native grid edges.

        Floors the south/west limits and ceils the north/east limits to the
        nearest multiple of the request's grid spacing (see
        :meth:`_grid_resolution_for_request`) — :data:`ERA5_GRID_DEGREES`
        (0.125°) for regular CDS datasets, or a dataset's own
        `grid_resolution` (e.g. GloFAS's 0.05° on EWDS) — so every retrieve
        aligns with the native grid and no cell straddles the requested area
        boundary.

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees north.
            lon_lim: `[lon_min, lon_max]` in degrees east.

        Returns:
            SpatialExtent: Grid-aligned bounding box with `resolution` set to
            the request's native grid spacing (see
            :meth:`_grid_resolution_for_request`) — the ERA5 default or a
            dataset's `grid_resolution`.

        Examples:
            - Snap a 1° box to the ERA5 grid:

                ```python
                >>> ecmwf = ECMWF.__new__(ECMWF)
                >>> extent = ecmwf._create_grid([4.19, 4.64], [-75.65, -74.73])
                >>> round(extent.resolution, 3)
                0.125
                >>> round(extent.latitude_min, 3), round(extent.latitude_max, 3)
                (4.125, 4.75)

                ```
            - The bbox always grows out to grid edges:

                ```python
                >>> ecmwf = ECMWF.__new__(ECMWF)
                >>> extent = ecmwf._create_grid([0.05, 0.95], [0.05, 0.95])
                >>> round(extent.latitude_min, 3), round(extent.latitude_max, 3)
                (0.0, 1.0)
                >>> round(extent.longitude_min, 3), round(extent.longitude_max, 3)
                (0.0, 1.0)

                ```
        """
        cell_size = self._grid_resolution_for_request()
        lat_lim_floor = np.floor(lat_lim[0] / cell_size) * cell_size
        lat_lim_ceil = np.ceil(lat_lim[1] / cell_size) * cell_size
        lat_lim = [lat_lim_floor, lat_lim_ceil]

        lon_lim_floor = np.floor(lon_lim[0] / cell_size) * cell_size
        lon_lim_ceil = np.ceil(lon_lim[1] / cell_size) * cell_size
        lon_lim = [lon_lim_floor, lon_lim_ceil]
        return SpatialExtent.from_pairs(
            lat_lim=lat_lim, lon_lim=lon_lim, resolution=cell_size
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
        errors: str = "warn",
    ) -> list[Path]:
        """Download every `(dataset, variable)` pair in `self.vars` from CDS.

        Iterates the user-supplied `variables` mapping (CDS dataset
        short name → list of variable codes) and, for each pair,
        looks the variable up in the CDS :class:`Catalog` and
        delegates to :meth:`_download_dataset`.

        Args:
            progress_bar: Reserved; currently unused since the
                slicing pipeline that previously consumed it has
                been moved out of the package. Defaults to `True`
                so existing callers keep working.
            aggregate: Optional :class:`earthlens.aggregate.AggregationConfig`.
                When provided, every retrieved NetCDF is fed through
                :func:`earthlens.aggregate.aggregate_netcdf` immediately
                after `_api()` returns. When the config's `out_dir`
                is `None`, it is defaulted to
                `<self.root_dir>/aggregated/`. Aggregation failures
                surface in the per-variable failure summary alongside
                retrieve failures, so a single bad variable does not
                abort the rest of the loop.

                **`op="auto"` semantics.** When the config's `op` is
                left at its default `"auto"`, the reducer is picked
                per-variable from the catalog row's `types` field
                (`Variable.is_flux`):

                * **State** (`types` unset or `"state"` — e.g.
                  `2m-temperature`, `surface-pressure`,
                  `relative-humidity`). Each NetCDF sample is the
                  instantaneous value at that timestamp. `auto` →
                  `"mean"`. The window mean is the natural daily /
                  monthly summary.
                * **Flux** (`types: flux` — e.g.
                  `total-precipitation`, `evaporation`,
                  `surface-runoff`, radiation accumulations). Each
                  NetCDF sample is the accumulation since the
                  previous post-processing step (a 6-hour
                  accumulation in legacy daily ERA5, 1-hour in
                  CDS-Beta). `auto` → `"sum"`. The per-slot
                  accumulations are summed inside the window to
                  recover the actual window total.

                Worked example — daily `evaporation` for one pixel
                with the four 6-hourly slots
                `[0.001, 0.002, 0.005, 0.004]` m of water
                equivalent. `op="auto"` resolves to `"sum"` and
                writes `0.012 m` (the day's total evaporation) to
                the GeoTIFF. A plain `op="mean"` would write
                `0.003 m` (the average 6-hour accumulation, **not**
                a daily total).

                * **Pre-aggregated** (`Variable.is_pre_aggregated` —
                  the `derived-era5-*-daily-statistics` and
                  `reanalysis-era5-*-monthly-means` families, where
                  each NetCDF sample is already a server-side daily /
                  monthly aggregate). `auto` → `"mean"`, overriding
                  the flux rule: a `"sum"` would re-accumulate the
                  aggregates and multiply by the number of samples in
                  the window (~30× for a monthly window over daily
                  statistics).

                Pass an explicit `op="mean"` / `"sum"` / `"min"` /
                `"max"` / `"std"` to bypass auto-routing entirely. See
                `docs/aggregation.md` for the full walkthrough.
        Returns:
            list[Path]: The written output paths — one per-variable
            NetCDF at `<self.root_dir>/<cds_variable>_<dataset_id>.nc`
            (`dataset_id` is the requested catalog id; it equals
            `cds_dataset` except for a row that overrides it — the GloFAS
            intermediate stream — which is named by its own id so it does
            not collide with the consolidated stream),
            or, when `aggregate` is set, the per-window GeoTIFFs at
            `<aggregate.out_dir or self.root_dir/aggregated>/<cds_variable>_<dataset_id>_<freq>_<window>.tif`
            (the `dataset_id` is carried for the same collision-avoidance reason
            as the `.nc` name above — datasets sharing a `cds_variable` reduce
            into one `aggregated/` dir and would otherwise overwrite one another).
            A zip-of-NetCDF response (satellite CDRs, CAMS `netcdf_zip`)
            that unpacks to more than one member is returned as every
            member under a sibling `<cds_variable>_<dataset_id>/`
            directory (all masked to a polygon `aoi=` if one was given);
            such a multi-member response cannot be aggregated. Under the
            default `errors="warn"`, variables whose download (or
            aggregate) failed are logged and omitted from the returned
            list rather than aborting the batch. A store-level refusal is
            the exception to that — see :class:`CadsUnavailableError`
            under `Raises:`.

        Raises:
            ValueError: If `errors` is not a recognised policy.
            KeyError: If any dataset key in `self.vars` is not a
                curated CDS dataset, or if a listed variable is not
                declared under that dataset — under `errors="warn"` this
                is logged per pair rather than raised.
            CadsUnavailableError: The store refused to queue the job on
                its per-dataset limit and kept refusing across all three
                attempts (roughly six seconds of backoff). Raised **whatever
                `errors` is set to**, including `"ignore"`: a refusal by
                the service is not the per-variable data gap the policy
                exists to absorb, and continuing would report an outage
                as every variable having no data.
            PermissionError: The dataset's licence has not been accepted
                on the Copernicus account.
            Exception: Any error :meth:`_api` propagates from
                :meth:`cdsapi.Client.retrieve`.

        Examples:
            - End-to-end download via the user-facing
              :class:`EarthLens` facade. Marked
              `# doctest: +SKIP` because it requires a configured
              `~/.cdsapirc` and several minutes of CDS queue time:

                ```python
                >>> from earthlens.earthlens import EarthLens
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="ecmwf",
                ...     temporal_resolution="daily",
                ...     start="2022-01-01",
                ...     end="2022-01-01",
                ...     variables={
                ...         "reanalysis-era5-single-levels": [
                ...             "2m-temperature", "total-precipitation"
                ...         ],
                ...     },
                ...     lat_lim=[4.0, 5.0],
                ...     lon_lim=[-75.0, -74.0],
                ...     path="examples/data/era5",
                ... )
                >>> earthlens.download()  # doctest: +SKIP

                ```

        See Also:
            :meth:`_download_dataset`: Per-variable download +
                post-processing.
            :meth:`_api`: Builds and submits the cdsapi request.
            :class:`Catalog`: Resolves `(dataset, code)` pairs to
                per-variable metadata.
        """
        if getattr(self, "_passthrough", None) is not None:
            return self._download_passthrough(aggregate=aggregate)
        self._errors = self.check_errors_policy(errors)
        catalog = Catalog()
        effective_aggregate: AggregationConfig | None = None
        if aggregate is not None:
            # Only the written paths are kept below, so the reduced arrays are
            # dropped as each window lands rather than accumulating: a daily
            # reduction of a decade-long request would otherwise hold every
            # window in memory alongside the GeoTIFFs already on disk.
            updates: dict[str, object] = {"keep_arrays": False}
            if aggregate.out_dir is None:
                updates["out_dir"] = self.root_dir / "aggregated"
            effective_aggregate = aggregate.model_copy(update=updates)

        assert isinstance(self.vars, dict)  # ECMWF requires a {dataset: [vars]} mapping
        pairs = [
            (dataset_name, var)
            for dataset_name, var_codes in self.vars.items()
            for var in var_codes
        ]
        per_pair_paths, failures = self._run_items(
            pairs,
            partial(
                self._download_pair,
                catalog=catalog,
                progress_bar=progress_bar,
                aggregate=effective_aggregate,
            ),
            errors=self._errors,
            label="variable",
            describe=_describe_pair,
            # A throttled store refused to serve anything; continuing would
            # report that as every variable having no data.
            fatal=(CadsUnavailableError,),
        )
        if not failures:
            logger.info(
                f"ECMWF download summary: all {len(pairs)} variables succeeded."
            )
        return [path for paths in per_pair_paths for path in paths]

    def _download_pair(
        self,
        pair: tuple[str, str],
        *,
        catalog: Catalog,
        progress_bar: bool,
        aggregate: AggregationConfig | None,
    ) -> list[Path]:
        """Retrieve one `(dataset, variable)` pair, aggregating if asked.

        A failure anywhere here — resolving the variable, the CDS retrieve, or
        the reduction — leaves the whole pair failed, which is what the caller's
        `errors=` policy is applied to.

        Args:
            pair: The `(dataset name, variable code)` to retrieve.
            catalog: The CDS catalog, resolving the pair to variable metadata.
            progress_bar: Forwarded to the per-request download.
            aggregate: The reduction to apply, or `None` to keep the NetCDF.

        Returns:
            list[Path]: The NetCDF path, or the per-window GeoTIFFs when
                aggregating.
        """
        dataset_name, var = pair
        logger.info(
            f"Download ECMWF {dataset_name}/{var} data for period "
            f"{self.time.start_date} till {self.time.end_date}"
        )
        var_info = catalog.get_variable(dataset_name, var)
        nc_path = self._download_dataset(var_info, progress_bar=progress_bar)
        # A multi-member zip-of-NetCDF (satellite CDR / CAMS `netcdf_zip` over
        # several timesteps) is unpacked by `_api` into this sibling
        # `<cds_variable>_<cds_dataset>/` directory; the returned member's parent
        # equals it exactly (a single-file retrieve returns the file at
        # `root_dir` itself).
        member_dir = (
            self.root_dir
            / f"{var_info.cds_variable}_{var_info.dataset_id or var_info.cds_dataset}"
        )
        is_multi_member = member_dir.is_dir() and nc_path.parent == member_dir
        if aggregate is None:
            # Return every member (already masked in `_api`) so `download()`'s
            # list is complete, not just the first.
            if is_multi_member:
                return sorted(member_dir.glob("*.nc"))
            return [nc_path]
        # `aggregate_netcdf` reduces a single cube — reducing only the first
        # member would return a plausible-looking but partial result, so refuse.
        if is_multi_member:
            raise ValueError(
                f"{dataset_name}/{var}: the retrieve returned a multi-member "
                "zip-of-NetCDF (one file per timestep); aggregating across "
                "members is not supported. Re-run without `aggregate=` and "
                f"reduce the member directory ({member_dir}) yourself, or "
                "request a single window."
            )
        # Bound the aggregation to the requested span: a daily CDS
        # `year`/`month`/`day` request is a cross-product, so a window
        # crossing month boundaries over-covers the range (a Jun 25-Jul 5
        # request also returns Jun 1-5 and Jul 25-30). Trimming here keeps the
        # written GeoTIFFs faithful to `start`/`end` and stops a stray day from
        # skewing a window it shares (e.g. a monthly mean).
        agg = aggregate_netcdf(
            nc_path,
            var_info,
            aggregate,
            date_range=(self.time.start_date, self.time.end_date),
        )
        return [path for _, _, path in agg if path is not None]

    def _resolve_endpoint(self, dataset: str) -> str:
        """Resolve which store hosts `dataset` for a passthrough retrieve.

        Delegates to :func:`earthlens.ecmwf._helpers.endpoint_for`, the one
        resolver the CLI tooling shares: a curated row's `endpoint` wins, then
        the per-store availability index, then `"cds"` with a warning.

        Args:
            dataset: The Copernicus dataset id being retrieved.

        Returns:
            str: The store slug (`"cds"` / `"ads"` / `"ewds"`).
        """
        return endpoint_for(dataset)

    def _passthrough_target(self, dataset: str, request: dict[str, Any]) -> str:
        """Pick an output filename for a raw retrieve from the request format.

        Args:
            dataset: The dataset id (becomes the filename stem).
            request: The raw request dict (its format hint picks the suffix).

        Returns:
            str: `<dataset><suffix>` — `.nc` / `.zip` / `.grib` / `.bin`.
        """
        fmt = str(request.get("data_format") or request.get("format") or "").lower()
        download_fmt = str(request.get("download_format") or "").lower()
        if download_fmt == "zip" or fmt == "netcdf_zip":
            suffix = ".zip"
        elif fmt == "netcdf":
            suffix = ".nc"
        elif fmt in ("grib", "grib2"):
            suffix = ".grib"
        else:
            suffix = ".bin"
        return f"{dataset}{suffix}"

    def _download_passthrough(
        self, aggregate: AggregationConfig | None = None
    ) -> list[Path]:
        """Retrieve a raw `dataset` + `request` from the resolved store.

        The coverage lever (`G3`): forwards a raw request to the endpoint
        router with no curated row, typed variable resolution, grid snap, or
        constraint pre-validation. NetCDF (incl. single-member `netcdf_zip`)
        is unwrapped in place; other formats (GRIB, multi-member zips, CSV) are
        written raw with a clear log for the caller / the C3 format handler.

        Args:
            aggregate: Rejected — aggregation needs a curated `Variable`.

        Returns:
            list[Path]: The written file(s) — one for a plain retrieve, or the
            member NetCDFs for a multi-member zip-of-NetCDF response.

        Raises:
            ValueError: If `aggregate` is set (it needs a curated `Variable`).
            PermissionError: If the store rejects the dataset for an unaccepted
                licence (mapped to name the dataset page).
        """
        if aggregate is not None:
            raise ValueError(
                "aggregate= needs a curated dataset row; it is not available "
                "for a raw-request passthrough."
            )
        spec = self._passthrough
        assert spec is not None
        dataset = spec["dataset"]
        request = spec["request"]
        endpoint = spec["endpoint"] or self._resolve_endpoint(dataset)
        target = self.root_dir / self._passthrough_target(dataset, request)
        client = self._client_for(endpoint)
        logger.info(
            f"Passthrough retrieve {dataset!r} via {endpoint.upper()}; "
            "this may take several minutes"
        )
        _retrieve_with_retry(client, dataset, request, target, endpoint)
        return self._passthrough_postprocess(target)

    def _passthrough_postprocess(self, target: Path) -> list[Path]:
        """Normalise a raw-retrieve output by its detected format.

        A zip-of-NetCDF is unpacked to its member NetCDF(s) (single-member in
        place, multi-member into a sibling directory); GRIB and plain NetCDF are
        left as written (pyramids reads both); an unrecognised blob (CSV/point)
        is left raw with a clear log for the tabular path (`C9`). Never raises on
        the archive shape — passthrough must not fail after a successful
        retrieve.

        Args:
            target: The file the retrieve wrote.

        Returns:
            list[Path]: The resulting file path(s).
        """
        output_format = _detect_output_format(target)
        if output_format == "zip":
            return _unpack_netcdf_archive(target)
        if output_format == "grib":
            logger.info(f"{target.name}: GRIB written (readable via pyramids/GDAL).")
        elif output_format == "unknown":
            logger.info(
                f"{target.name}: non-raster format (CSV/point observations) "
                "written raw. In-situ / point datasets stay passthrough-only — "
                "read the file directly (e.g. with pandas)."
            )
        else:
            logger.info(
                f"{target.name}: NetCDF written ({target.stat().st_size} bytes)."
            )
        return [target]

    def _download_dataset(
        self,
        var_info: Variable,
        progress_bar: bool = True,
    ):
        """Download a single variable from CDS.

        Thin wrapper around :meth:`_api` — builds the cdsapi request,
        submits it, and returns the absolute :class:`pathlib.Path`
        to the NetCDF that CDS wrote.

        Per-date GeoTIFF slicing is **not** done here. Users who
        want per-date `.tif` outputs can run
        `examples/post_process_ecmwf_netcdf.py` against the
        returned NetCDF.

        Args:
            var_info: Catalog row for the variable. See :meth:`_api`
                for the attributes consumed.
            progress_bar: Reserved; currently unused since the
                slicing pipeline that previously consumed it has
                been moved out of the package. Defaults to `True`
                so existing callers keep working.

        Returns:
            pathlib.Path: Absolute path to the downloaded NetCDF.

        See Also:
            :meth:`_api`: Builds and submits the CDS request, returns
                the path to the NetCDF.
            :class:`Catalog`: Loads `Variable` instances from the
                bundled CDS catalog (`catalog/`).
        """
        return self._api(var_info)

    def _api(self, var_info: Variable):
        """Submit a CDS retrieve request for one variable and return the path.

        Five-stage pipeline:

        1. Derive the dataset name from `var_info.cds_dataset`.
        2. Delegate request-dict assembly to :meth:`_build_request`.
        3. Pre-flight the request via
           :class:`earthlens.ecmwf.constraints.RequestValidator`
           (skipped when the constructor was given
           `skip_constraints=True`).
        4. Submit via :meth:`cdsapi.Client.retrieve`. The call blocks
           until CDS has served the request and written the NetCDF
           — typically minutes due to CDS queue times.
        5. On failure, rewrite licence-not-accepted exceptions into a
           :class:`PermissionError` carrying the dataset's licence
           page URL. All other exceptions propagate untouched.

        Output filename:
        `<self.root_dir>/<cds_variable>_<cds_dataset>.nc`.

        Args:
            var_info: Catalog row resolved by :class:`Catalog`.
                See :meth:`_build_request` for the full list of
                fields consumed during request assembly. `_api`
                itself reads `cds_dataset` (the retrieve target) and
                `cds_variable` / `dataset_id` (the output filename stem).

        Returns:
            pathlib.Path: Absolute path to the downloaded NetCDF
            file.

        Raises:
            PermissionError: When CDS rejects the request because
                the dataset's licence has not been accepted on the
                user's CDS account. Message links to the dataset's
                licence page.
            ValueError: Propagated from
                :class:`earthlens.ecmwf.constraints.RequestValidator`
                when the assembled request fails the pre-flight
                check (variable typo, unknown extras, malformed
                date / area, ...). Skipped entirely when
                `skip_constraints=True`.
            Exception: Other transport-level errors from
                :meth:`cdsapi.Client.retrieve` (authentication
                failures, transient CDS 5xx, network drops)
                propagate untouched.

        Examples:
            - Inspect the variable + filename pattern this method
              produces (no network access — pure catalog read):

                ```python
                >>> from earthlens.ecmwf import Catalog
                >>> spec = Catalog().get_variable(
                ...     "reanalysis-era5-single-levels", "2m-temperature"
                ... )
                >>> spec.cds_dataset
                'reanalysis-era5-single-levels'
                >>> spec.dataset_id == spec.cds_dataset  # equal for ordinary rows
                True
                >>> f"{spec.cds_variable}_{spec.dataset_id}.nc"
                '2m_temperature_reanalysis-era5-single-levels.nc'

                ```
            - Submit the request through the user-facing
              :class:`EarthLens` facade. Marked
              `# doctest: +SKIP` because it requires a configured
              `~/.cdsapirc` and several minutes of CDS queue time:

                ```python
                >>> from earthlens.earthlens import EarthLens  # doctest: +SKIP
                >>> earthlens = EarthLens(  # doctest: +SKIP
                ...     data_source="ecmwf",
                ...     temporal_resolution="daily",
                ...     start="2022-01-01",
                ...     end="2022-01-01",
                ...     variables={
                ...         "reanalysis-era5-single-levels": ["2m-temperature"],
                ...     },
                ...     lat_lim=[4.0, 5.0],
                ...     lon_lim=[-75.0, -74.0],
                ...     path="examples/data/era5",
                ... )
                >>> earthlens.download()  # doctest: +SKIP

                ```

        See Also:
            :meth:`_build_request`: Assembles the CDS request dict
                this method submits — the pure-builder collaborator.
            :class:`earthlens.ecmwf.constraints.RequestValidator`: The
                pre-flight check applied to the assembled request.
            :meth:`_download_dataset`: Thin pass-through wrapper —
                calls this method and returns the same path.
            :class:`Catalog`: Resolves `(dataset, variable)` pairs
                to :class:`Variable` rows.
            :class:`earthlens.earthlens.EarthLens`: User-facing facade
                that wires this method into the `download()` flow.
        """
        dataset = var_info.cds_dataset
        request = self._build_request(var_info)

        # Pre-flight check the assembled request against the CDS
        # `constraints.json` for this dataset. Catches typos and
        # invalid extras combinations client-side before they
        # consume a CDS queue slot. Pass `skip_constraints=True`
        # to `ECMWF(...)` to bypass.
        RequestValidator(
            dataset,
            request,
            skip=self.skip_constraints,
            base_url=constraints_base_url(var_info.endpoint),
        ).check()

        # Name the output by the requested catalog id (dataset_id), not the
        # retrieve target (cds_dataset). They match for every ordinary row; they
        # differ only when a row overrode cds_dataset (the GloFAS intermediate
        # stream), and naming by dataset_id stops its file from colliding with
        # the consolidated stream, which shares cds_variable + cds_dataset.
        stem = f"{var_info.cds_variable}_{var_info.dataset_id or dataset}"
        target = self.root_dir / f"{stem}.nc"
        client = self._client_for(var_info.endpoint)
        logger.info(
            f"Requesting {dataset} from {var_info.endpoint.upper()}; "
            "this may take several minutes"
        )
        _retrieve_with_retry(client, dataset, request, target, var_info.endpoint)
        # A zip-of-NetCDF response (satellite CDRs, CAMS netcdf_zip) is unpacked
        # by the C3 handler — single-member in place, multi-member into a
        # sibling dir — so a multi-timestep curated retrieve does not crash.
        members = _unpack_netcdf_archive(target)
        if len(members) > 1:
            logger.warning(
                f"{dataset}: retrieve returned {len(members)} NetCDF members "
                f"(unpacked to {members[0].parent}). A plain download returns "
                "every member; a multi-member curated aggregate is not supported."
            )
        # Mask every member (not just the first) so a polygon `aoi=` trims the
        # whole time series, and return the primary — `_download_pair` fans the
        # sibling dir out into the full member list for a plain download.
        for member in members:
            self._mask_netcdf_to_geometry(member)
        return members[0]

    def _mask_netcdf_to_geometry(self, target: Path) -> None:
        """Mask a written NetCDF cube to a polygon `aoi=`, if one was given.

        The CDS `area` field already crops server-side to the bbox; this
        trims the bbox corners to the exact polygon when the request's
        `aoi=` was a polygon (carried on `self.space.geometry`). Every
        variable / time slice is masked via `pyramids.NetCDF.crop`, written
        through a sibling temp file that atomically replaces the original
        so a partial write cannot corrupt the cube. A no-op for a bbox /
        point `aoi=`.

        pyramids carries the CDS cube's non-spatial aux variables (ERA5's
        `expver` / `number`) through the crop — numeric and string alike
        (serapeum-org/pyramids#514, #567) — so the mask applies cleanly; any
        genuine error (e.g. a polygon that does not overlap the data) is left
        to propagate.

        Args:
            target: Path to the NetCDF written by `_api`.
        """
        geometry = getattr(self.space, "geometry", None)
        if geometry is None:
            return
        from pyramids.netcdf import NetCDF

        cube = NetCDF.read_file(str(target))
        masked = None
        tmp = target.with_name(target.stem + ".masked" + target.suffix)
        wrote_tmp = False
        try:
            masked = cube.crop(mask=geometry, touch=True)
            masked.to_file(str(tmp))
            wrote_tmp = True
        finally:
            cube.close()
            if masked is not None:
                masked.close()
            if not wrote_tmp:
                tmp.unlink(missing_ok=True)
        os.replace(tmp, target)

    def _build_request(self, var_info: Variable) -> dict[str, Any]:
        """Assemble the CDS retrieve-request dict for one variable.

        Pure function over `var_info`, `self.time.dates`,
        `self.space`, and `self.temporal_resolution`. No I/O, no
        validation, no client calls — just dictionary assembly.
        :meth:`_api` consumes the result and submits it via
        :meth:`cdsapi.Client.retrieve`.

        Build order (later steps override earlier ones):

        1. Template defaults (`variable`, `year`, `month`,
           `data_format`, `area`, `product_type`).
        2. Daily / monthly branch — daily adds `day` plus four
           six-hourly `time` slots; monthly pins `time=["00:00"]`
           and omits `day` (CDS monthly-means datasets reject
           `day`).
        3. Pressure-level forward — `cds_pressure_level` becomes
           `pressure_level` on the request, unless the retrieval set
           `pressure_level=`, which replaces it. A row the catalog gives
           no level keeps none either way.
        4. `var_info.extras` merge — per-row catalog overrides win
           over the template defaults, but not over the retrieval's own
           `pressure_level=`, which is applied after them.
        5. `request_kind` strip — drop template-default keys the
           dataset family rejects (e.g. ORAS5 rejects
           `day`/`time`/`area`). Done after the extras merge so a
           user can re-introduce a stripped key by setting it
           explicitly in extras.
        6. Per-row `None` opt-outs — any `extras` key set to `None`
           is dropped from the request, the per-row escape hatch
           for datasets that reject the default bbox without
           forcing a new `request_kind`.

        Args:
            var_info: Catalog row for the variable being requested.
                Drives every field on the request except `area` /
                `year` / `month` / `day` / `time` (which come from
                `self.space` and `self.time`).

        Returns:
            dict[str, Any]: Request dict ready to pass as the
            second positional argument to
            :meth:`cdsapi.Client.retrieve`.
        """
        if (
            var_info.request_kind in ("glofas", "glofas_hindcast")
            and self.temporal_resolution == "monthly"
        ):
            raise ValueError(
                f"{var_info.cds_dataset!r} (GloFAS) must be requested with "
                "temporal_resolution='daily': the monthly branch omits the "
                "'day' selector that the forecast-reference date requires. "
                "Set temporal_resolution='daily'."
            )
        dates = self.time.dates
        request: dict[str, Any] = {
            "variable": [var_info.cds_variable],
            "year": sorted({str(d.year) for d in dates}),
            "month": sorted({f"{d.month:02d}" for d in dates}),
            "data_format": "netcdf",
            "area": [
                self.space.north,
                self.space.west,
                self.space.south,
                self.space.east,
            ],
        }
        # `product_type` only for datasets that declare one — CAMS keys on
        # `type`/`quantity` instead and rejects an empty `product_type`.
        if var_info.product_type:
            request["product_type"] = var_info.product_type

        if self.temporal_resolution == "monthly":
            request["time"] = ["00:00"]
        else:
            request["day"] = sorted({f"{d.day:02d}" for d in dates})
            request["time"] = ["00:00", "06:00", "12:00", "18:00"]

        # Request-kind date representation (G11): cams_date → a single `date`
        # range string; the hindcast families remap year/month/day →
        # hyear/hmonth/hday (G7).
        _apply_request_kind_dates(
            request, var_info, self.time.start_date, self.time.end_date
        )

        if var_info.cds_pressure_level is not None:
            # Copy by value: the catalog row is cached process-wide, so
            # assigning its list would let an edit to one request rewrite the
            # level for every later retrieve of that variable.
            request["pressure_level"] = list(var_info.cds_pressure_level)

        _apply_extras_and_strips(request, var_info)

        # The retrieval's own level is applied last, after extras. A row may
        # carry its level in either place — the CARRA means rows keep theirs in
        # `extras` and leave `cds_pressure_level` unset — and extras are merged
        # with `update`, so an override written before this point would be put
        # back to the catalog's level for those rows and the caller would be
        # served a different altitude than they asked for, with no error.
        #
        # Keying on the assembled request rather than on the catalog row makes
        # both sources behave alike, and keeps the single-level case safe: such
        # a request never has the key, so it never acquires one.
        if self.pressure_level is not None:
            if "pressure_level" in request:
                request["pressure_level"] = list(self.pressure_level)
            else:
                # Silence here would cost a queue slot and return the wrong
                # thing: a single-level dataset served at the surface, or a
                # model-level row served at model level 1, with the override
                # accepted and never used.
                logger.warning(
                    f"pressure_level={self.pressure_level} does not apply to "
                    f"{var_info.cds_variable!r}: it is not requested on "
                    "pressure levels, so the override was not used."
                )

        return request
