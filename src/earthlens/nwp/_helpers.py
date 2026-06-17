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
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

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
    out: list[dt.datetime] = []
    day = dt.datetime(start.year, start.month, start.day)
    last = dt.datetime(end.year, end.month, end.day)
    while day <= last:
        for hour in hours:
            out.append(day.replace(hour=hour))
        day += dt.timedelta(days=1)
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
    import pandas as pd

    index = pd.DatetimeIndex(pd.to_datetime(list(times)))
    positions = pd.Series(range(len(index)), index=index)
    label_for: dict[int, str] = {}
    for window_start, group in positions.groupby(pd.Grouper(freq=freq)):
        if group.empty:
            continue
        label = window_start.strftime("%Y%m%d%H")
        for pos in group.tolist():
            label_for[int(pos)] = label
    return [label_for[i] for i in range(len(index))]


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
    return Path(platformdirs.user_cache_dir("earthlens")) / "nwp" / "idx"


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


def _parse_idx_noaa(text: str) -> pd.DataFrame:
    """Parse a NOAA `:`-separated `.idx` into the canonical idx frame.

    NOAA NODD writes one message per line: `msg : offset : date :
    abbr : level : forecast` (e.g. ` 1:0:d=2024060100:HGT:1000 mb:anl`).
    `length` is derived from the next message's offset; the trailing
    message gets `-1` (read-to-end).

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
        rows.append((msg_id, offset, parts[3].strip(), parts[4].strip(), parts[5].strip()))
    if not rows:
        return pd.DataFrame(columns=list(_IDX_COLUMNS))
    frame = pd.DataFrame(rows, columns=["msg_id", "offset", "var", "level", "step"])
    frame = frame.sort_values("offset").reset_index(drop=True)
    nxt = frame["offset"].shift(-1)
    frame["length"] = (nxt - frame["offset"]).fillna(-1).astype("int64")
    return frame[list(_IDX_COLUMNS)]


def _parse_idx_ecmwf(text: str) -> pd.DataFrame:
    """Parse the ECMWF Open Data newline-JSON index into the canonical frame.

    Each non-empty line is one JSON object with `_offset` / `_length`
    byte-range fields plus the variable selectors (`param`,
    `levelist`, `step`). Malformed lines are skipped.

    Args:
        text: The raw `.index` body (one JSON object per line).

    Returns:
        pandas.DataFrame: Rows with the canonical columns. Empty when
            no parseable line is present.
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
        try:
            offset = int(obj.get("_offset", -1))
            length = int(obj.get("_length", -1))
        except (TypeError, ValueError):
            continue
        rows.append(
            (
                i,
                offset,
                length,
                str(obj.get("param", "")),
                str(obj.get("levelist", "")),
                str(obj.get("step", "")),
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
    `platformdirs.user_cache_dir("earthlens") / "nwp" / "idx"` with
    a stable per-URL filename and a 24-hour TTL; the TTL is overridable
    via the `EARTHLENS_NWP_IDX_TTL` environment variable, or by an
    explicit `ttl=` keyword that wins over both.

    The current centre modules delegate `.idx` reads to their SDKs
    (Herbie owns NOAA, `ecmwf-opendata.Client` owns ECMWF Open Data);
    this helper is the cache contract for any future first-party
    direct-fetch path (a non-SDK centre, or a direct retry shim
    around an SDK that does not cache).

    Args:
        url: The full URL of the `.idx` / `.index` file (used as
            the cache key after SHA-256 truncation).
        downloader: Callable that fetches the body of `url` and
            writes it to the given `Path`. Injected so tests can
            count calls and bypass the network.
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
    if path.exists():
        try:
            if (time.time() - path.stat().st_mtime) < ttl_seconds:
                frame = _parse_idx(path.read_text(encoding="utf-8", errors="replace"))
                if not frame.empty:
                    return frame
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    downloader(url, path)
    return _parse_idx(path.read_text(encoding="utf-8", errors="replace"))
