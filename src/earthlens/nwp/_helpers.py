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
from collections.abc import Iterable
from pathlib import Path


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


def cog_name(model_key: str, cycle: dt.datetime, step: int) -> str:
    """Return the output COG filename for one `(model, cycle, step)`.

    The naming convention `{model_key}_{cycle:%Y%m%d%H}_f{step:03d}.tif`
    is shared by every centre so the `_fetch` pipeline and the
    `aggregate=` window-labeller can both parse it back.

    Args:
        model_key: The catalog model key (e.g. `"gfs"`).
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.

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
    """
    return f"{model_key}_{cycle:%Y%m%d%H}_f{step:03d}.tif"


def grib_name(model_key: str, cycle: dt.datetime, step: int) -> str:
    """Return the intermediate GRIB2 filename for one `(model, cycle, step)`.

    Args:
        model_key: The catalog model key (e.g. `"icon-global"`).
        cycle: The forecast cycle datetime (UTC).
        step: The forecast lead time in hours.

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
    return f"{model_key}_{cycle:%Y%m%d%H}_f{step:03d}.grib2"


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
