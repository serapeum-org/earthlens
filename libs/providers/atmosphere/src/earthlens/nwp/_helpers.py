"""Provider-agnostic helpers for the NWP backend.

Pure functions shared by the centre modules and the `NWP` backend:
the cycle-grid walk that turns a `(start, end)` date range plus a
model's `cycles_utc` into the concrete UTC run datetimes, and the
output-path naming convention for the one-COG-per-`(cycle, step)`
artefacts. None of these touch the network or any optional SDK, so
they import cleanly without the `[nwp]` extra.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from earthlens.config import cache_dir

if TYPE_CHECKING:
    import pandas as pd

#: Default `.idx` cache time-to-live in seconds (24 hours). The
#: `EARTHLENS_NWP_IDX_TTL` environment variable overrides this;
#: an explicit `ttl=` keyword to :func:`get_idx` wins over both.
_DEFAULT_IDX_TTL = 24 * 3600

#: The columns every `.idx` parser returns. NOAA text and ECMWF JSON
#: collapse to one shape so downstream filters (`var`, `level`,
#: `step`) work without minding which provider the index came from.
_IDX_COLUMNS = ("msg_id", "offset", "length", "var", "level", "step")


def enumerate_cycles(
    start: dt.datetime,
    end: dt.datetime,
    cycles_utc: Iterable[int],
) -> list[dt.datetime]:
    """Enumerate the forecast cycles a model runs within a date range.

    A numerical-weather-prediction model runs a fixed set of cycles
    per day (the run hours in `cycles_utc`, e.g. `[0, 6, 12, 18]`).
    This walks every calendar day from `start` to `end` inclusive and
    emits one timezone-naive UTC datetime per run hour on that day,
    sorted ascending.

    Args:
        start: Inclusive start of the cycle-date range (date or
            datetime; only the calendar date is used for the lower
            bound).
        end: Inclusive end of the cycle-date range.
        cycles_utc: The model's daily run hours, in `[0, 23]`.

    Returns:
        list[datetime.datetime]: One naive datetime per
            `(day, run-hour)` in range, ascending.

    Raises:
        ValueError: If `start` is later than `end`, or a run hour is
            outside `[0, 23]`.

    Examples:
        - Enumerate the two cycles of a single day:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import enumerate_cycles
            >>> enumerate_cycles(dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1), [0, 12])
            [datetime.datetime(2024, 6, 1, 0, 0), datetime.datetime(2024, 6, 1, 12, 0)]

            ```
        - Three days of 4 cycles each yields twelve datetimes:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import enumerate_cycles
            >>> cycles = enumerate_cycles(dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 3), [0, 6, 12, 18])
            >>> len(cycles)
            12
            >>> cycles[0]
            datetime.datetime(2024, 6, 1, 0, 0)

            ```
    """
    if start > end:
        raise ValueError(f"start ({start}) is after end ({end}).")
    hours = sorted(set(cycles_utc))
    for hour in hours:
        if not 0 <= hour <= 23:
            raise ValueError(f"cycle hour {hour} is outside [0, 23].")
    from earthlens.base import date_windows

    out: list[dt.datetime] = []
    for day in date_windows(start.date(), end.date(), "D"):
        for hour in hours:
            out.append(dt.datetime(day.year, day.month, day.day, hour))
    return out


def cog_name(
    model_key: str, cycle: dt.datetime, step: int, member: str | None = None
) -> str:
    """Return the output COG filename for one `(model, cycle, step[, member])`.

    The naming convention `{model_key}_{cycle:%Y%m%d%H}_f{step:03d}.tif`
    (with an optional `_m{member}` before the suffix) is shared by every
    centre so the `_fetch` pipeline and the `aggregate=` window-labeller
    can both parse it back.

    Args:
        model_key: The catalog model key (e.g. `"gfs"`).
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.
        member: Ensemble member id, or `None` for a deterministic model
            (in which case no member suffix is added).

    Returns:
        str: The COG filename (no directory).

    Examples:
        - Name the 24 h COG of the 2024-06-01 12Z GFS run:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import cog_name
            >>> cog_name("gfs", dt.datetime(2024, 6, 1, 12), 24)
            'gfs_2024060112_f024.tif'

            ```
        - An ensemble member adds a `_m{member}` suffix:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import cog_name
            >>> cog_name("gefs", dt.datetime(2024, 6, 1, 0), 6, member="p01")
            'gefs_2024060100_f006_mp01.tif'

            ```
    """
    suffix = f"_m{member}" if member is not None else ""
    return f"{model_key}_{cycle:%Y%m%d%H}_f{step:03d}{suffix}.tif"


def grib_name(
    model_key: str, cycle: dt.datetime, step: int, member: str | None = None
) -> str:
    """Return the intermediate GRIB2 filename for one `(model, cycle, step)`.

    Args:
        model_key: The catalog model key (e.g. `"icon-global"`).
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.
        member: Ensemble member id, or `None` (no member suffix).

    Returns:
        str: The GRIB2 filename (no directory).

    Examples:
        - Name the analysis-step GRIB2 of the 2024-06-01 00Z ICON run:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import grib_name
            >>> grib_name("icon", dt.datetime(2024, 6, 1, 0), 0)
            'icon_2024060100_f000.grib2'

            ```
    """
    suffix = f"_m{member}" if member is not None else ""
    return f"{model_key}_{cycle:%Y%m%d%H}_f{step:03d}{suffix}.grib2"


def valid_time(cycle: dt.datetime, step: int) -> dt.datetime:
    """Return the valid time of a forecast = `cycle + step` hours.

    Args:
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.

    Returns:
        datetime.datetime: The instant the forecast is valid for.

    Examples:
        - A 30 h forecast from the 2024-06-01 00Z run is valid at 06Z next day:
            ```python
            >>> import datetime as dt
            >>> from earthlens.nwp._helpers import valid_time
            >>> valid_time(dt.datetime(2024, 6, 1, 0), 30)
            datetime.datetime(2024, 6, 2, 6, 0)

            ```
    """
    return cycle + dt.timedelta(hours=step)


def parse_cog_valid_time(path: Path | str) -> dt.datetime:
    """Recover a forecast's valid time from a `cog_name` filename.

    Inverts :func:`cog_name`: the trailing `_{cycle}_f{step}` of the stem
    gives the cycle datetime and lead time, whose sum is the valid time.
    Model keys may contain hyphens but never underscores, so the last two
    underscore-separated tokens are always the cycle stamp and the step.

    Args:
        path: A COG path or filename produced by :func:`cog_name`.

    Returns:
        datetime.datetime: The instant the forecast is valid for.

    Examples:
        - Recover the valid time of a 24 h forecast COG:
            ```python
            >>> from earthlens.nwp._helpers import parse_cog_valid_time
            >>> parse_cog_valid_time("gfs_2024060112_f024.tif")
            datetime.datetime(2024, 6, 2, 12, 0)

            ```
    """
    stem = Path(path).stem
    _, cycle_str, step_str = stem.rsplit("_", 2)
    cycle = dt.datetime.strptime(cycle_str, "%Y%m%d%H")
    return valid_time(cycle, int(step_str.lstrip("f")))


def window_labels(times: list[dt.datetime], freq: str) -> list[str]:
    """Return one `YYYYMMDDHH` window-start label per time, bucketed by `freq`.

    Times sharing a `freq` window get the same label, so
    `DatasetCollection.groupby` coarsens the forecast time axis to one
    slice per window. The hour is kept in the label so sub-daily windows
    (e.g. `"6h"`) on the same day stay distinct.

    Args:
        times: Valid (or cycle) times, in file order.
        freq: A pandas offset alias (`"6h"`, `"1D"`, `"1MS"`, …).

    Returns:
        list[str]: One label per input time (same length as `times`).
    """
    from earthlens.base import window_labels as _base_window_labels

    return _base_window_labels(times, freq, fmt="%Y%m%d%H")


def ensure_dir(path: Path | str) -> Path:
    """Create `path` if absent and return it as an absolute `Path`.

    Args:
        path: A directory path.

    Returns:
        pathlib.Path: The absolute, existing directory.
    """
    p = Path(path).absolute()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _idx_cache_root() -> Path:
    """Return the user's cache directory for cached NWP `.idx` files."""
    return cache_dir() / "nwp" / "idx"


def _key_from_url(url: str) -> str:
    """Stable, filesystem-safe cache key for a `.idx` URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.idx"


def _idx_cache_path(url: str) -> Path:
    """Absolute on-disk path where this URL's `.idx` is cached."""
    return _idx_cache_root() / _key_from_url(url)


def _resolve_idx_ttl(ttl: float | None) -> float:
    """Resolve the TTL precedence — `ttl=` kwarg > env var > default."""
    if ttl is not None:
        return float(ttl)
    env = os.getenv("EARTHLENS_NWP_IDX_TTL")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return float(_DEFAULT_IDX_TTL)


_NOAA_FCST_RE = re.compile(r"^(\d+)(?:-\d+)?\s*hour", re.IGNORECASE)


def _parse_noaa_step(token: str) -> int:
    """Map a NOAA forecast-time token to an integer lead-time in hours.

    The NOAA `.idx` forecast field is human-readable text — `anl` for the
    analysis step, `6 hour fcst` for an `f006` deterministic step,
    `0-3 hour acc fcst` for the start of a 0-3 h accumulation window.
    All shapes collapse to a single non-negative integer so the
    canonical `step` column is comparable across NOAA and ECMWF.

    Args:
        token: The raw `:`-separated forecast field.

    Returns:
        int: The forecast lead time in hours. `0` for `anl` / blank;
            the leading hour count for any `N hour …` shape; `-1` for
            anything unrecognised (kept as an explicit sentinel rather
            than silently mapped to `0`).
    """
    stripped = token.strip().lower()
    if not stripped or stripped == "anl":
        return 0
    match = _NOAA_FCST_RE.match(stripped)
    if match:
        return int(match.group(1))
    return -1


def _parse_idx_noaa(text: str) -> pd.DataFrame:
    """Parse a NOAA `:`-separated `.idx` into the canonical idx frame.

    NOAA NODD writes one message per line: `msg : offset : date :
    abbr : level : forecast` (e.g. ` 1:0:d=2024060100:HGT:1000 mb:anl`).
    The forecast field is parsed through :func:`_parse_noaa_step` so the
    `step` column is an `int` comparable with the ECMWF parser's output.
    `length` is derived from the next distinct offset; duplicate offsets
    (the same byte position appearing more than once) are dropped so a
    duplicate does not collapse the row to `length=0`. The trailing
    message keeps `length=-1` (read to end of file).

    Args:
        text: The raw `.idx` body.

    Returns:
        pandas.DataFrame: Rows with columns
            `(msg_id, offset, length, var, level, step)` sorted by
            `offset`. Empty when no parseable line is present.
    """
    import pandas as pd

    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            msg_id = int(parts[0])
            offset = int(parts[1])
        except ValueError:
            continue
        rows.append(
            (
                msg_id,
                offset,
                parts[3].strip(),
                parts[4].strip(),
                _parse_noaa_step(parts[5]),
            )
        )
    if not rows:
        return pd.DataFrame(columns=list(_IDX_COLUMNS))
    frame = pd.DataFrame(rows, columns=["msg_id", "offset", "var", "level", "step"])
    # Sort by offset and drop duplicate offsets — keep the first occurrence
    # so a sub-message sharing an envelope offset does not yield `length=0`.
    frame = (
        frame.sort_values("offset")
        .drop_duplicates("offset", keep="first")
        .reset_index(drop=True)
    )
    # Compute `length` directly in the loop to avoid the float intermediate
    # `(nxt - frame["offset"]).fillna(-1).astype("int64")` would produce.
    offsets = frame["offset"].tolist()
    lengths = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)] + [-1]
    frame["length"] = lengths
    return frame[list(_IDX_COLUMNS)]


def _parse_idx_ecmwf(text: str) -> pd.DataFrame:
    """Parse the ECMWF Open Data newline-JSON index into the canonical frame.

    Each non-empty line is one JSON object with `_offset` / `_length`
    byte-range fields plus the variable selectors (`param`,
    `levelist`, `step`). Malformed lines (invalid JSON, missing
    `_offset`, missing `_length`, non-numeric step) are skipped — they
    cannot produce a usable byte-range and must not poison the
    canonical schema with the NOAA "read to EOF" sentinel `-1`.

    Args:
        text: The raw `.index` body (one JSON object per line).

    Returns:
        pandas.DataFrame: Rows with the canonical columns. Empty when
            no parseable line is present. `length=-1` is reserved for
            the NOAA tail message (read-to-EOF); this parser never
            emits it.
    """
    import pandas as pd

    rows = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # An entry without an `_offset` or `_length` cannot drive a Range
        # fetch — drop it rather than fall back to -1, which would
        # collide with the NOAA tail sentinel downstream.
        if "_offset" not in obj or "_length" not in obj:
            continue
        try:
            offset = int(obj["_offset"])
            length = int(obj["_length"])
        except (TypeError, ValueError):
            continue
        try:
            step = int(obj.get("step", 0))
        except (TypeError, ValueError):
            continue
        rows.append(
            (
                i,
                offset,
                length,
                str(obj.get("param", "")),
                str(obj.get("levelist", "")),
                step,
            )
        )
    if not rows:
        return pd.DataFrame(columns=list(_IDX_COLUMNS))
    return pd.DataFrame(rows, columns=list(_IDX_COLUMNS))


def _parse_idx(text: str) -> pd.DataFrame:
    """Auto-detect NOAA-text vs ECMWF-JSON and dispatch.

    The first non-whitespace character of an ECMWF `.index` body is
    `{` (JSON object); NOAA `.idx` starts with a numeric message id
    followed by a colon. An empty/whitespace-only body returns the
    empty canonical frame.
    """
    stripped = text.lstrip()
    if not stripped:
        import pandas as pd

        return pd.DataFrame(columns=list(_IDX_COLUMNS))
    return _parse_idx_ecmwf(text) if stripped[0] == "{" else _parse_idx_noaa(text)


def get_idx(
    url: str,
    downloader: Callable[[str, Path], None],
    *,
    ttl: float | None = None,
) -> pd.DataFrame:
    """Fetch the `.idx` byte-range index for a URL, caching it on disk.

    A repeated multi-step / multi-cycle fetch hits the network once
    per index instead of once per request. The cache lives under
    `<cache_dir()>/nwp/idx` (the shared earthlens cache directory) with
    a stable per-URL filename and a 24-hour TTL; the TTL is overridable
    via the `EARTHLENS_NWP_IDX_TTL` environment variable, or by an
    explicit `ttl=` keyword that wins over both.

    The current centre modules delegate `.idx` reads to their SDKs
    (Herbie owns NOAA, `ecmwf-opendata.Client` owns ECMWF Open Data);
    this helper is the cache contract for any future first-party
    direct-fetch path (a non-SDK centre, or a direct retry shim
    around an SDK that does not cache).

    Writes are **atomic**: the downloader writes to a sibling
    temporary file and the helper renames it onto the cache path only
    on success, so a mid-download failure never leaves a truncated
    cache file shadowing the previous good copy.

    Args:
        url: The full URL of the `.idx` / `.index` file (used as
            the cache key after SHA-256 truncation).
        downloader: A callable that fetches the body of `url` and
            writes it to the given `Path`. The helper has no transport
            dependency itself, so any HTTP / S3 / file-copy implementation
            can be plugged in.
        ttl: Optional override for the cache TTL in seconds. `None`
            (the default) consults `EARTHLENS_NWP_IDX_TTL`, falling
            back to `_DEFAULT_IDX_TTL` (24 h).

    Returns:
        pandas.DataFrame: The parsed index with the canonical columns
            `(msg_id, offset, length, var, level, step)`.

    Notes:
        A cached file that parses to an empty frame (corrupt /
        truncated body) is treated as a miss and re-fetched once,
        rather than raising.
    """
    path = _idx_cache_path(url)
    ttl_seconds = _resolve_idx_ttl(ttl)
    # Any `OSError` while probing the cache (file racing with cleanup, a
    # permission flap, an unexpected errno on `path.exists()`) collapses into
    # a miss so the helper re-fetches rather than propagating an opaque
    # `stat`/`read` failure up through user code.
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < ttl_seconds:
            frame = _parse_idx(path.read_text(encoding="utf-8", errors="replace"))
            if not frame.empty:
                return frame
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write through a sibling temp file and atomically replace on success.
    # A downloader exception leaves the existing cache (if any) untouched;
    # the temp file is unlinked in the cleanup path.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".part", dir=path.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        downloader(url, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return _parse_idx(path.read_text(encoding="utf-8", errors="replace"))
