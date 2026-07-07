"""Flexible parsing of user-supplied start / end dates.

Backends historically required `start` / `end` as strings parsed with an
explicit `strptime` `fmt`. This helper lets the same call sites accept a
`datetime`, a `date`, a `pandas.Timestamp`, or a string — and parse the
string as ISO-8601 when the caller's `fmt` does not match — so the `fmt`
parameter becomes an optional override rather than a requirement.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd


def _strip_tz(value: dt.datetime) -> dt.datetime:
    """Return `value` as a naive `datetime`, converting to UTC first if aware.

    Keeps :func:`to_datetime` output uniformly tz-naive: a timezone-aware
    input is converted to UTC and stripped, so mixing (for example) a naive
    `start` with an offset-bearing `end` never raises the
    "can't compare offset-naive and offset-aware datetimes" `TypeError`
    downstream.

    Args:
        value: The `datetime` to normalize; may be naive or aware.

    Returns:
        A naive `datetime` (the UTC wall-clock time when `value` was aware).
    """
    if value.tzinfo is not None:
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def to_datetime(value: Any, fmt: str | None = None) -> dt.datetime:
    """Coerce a date-like value into a `datetime.datetime`.

    Accepts the rich set of inputs the popular EO packages accept, while
    staying backward compatible with the legacy `strptime`-with-`fmt`
    string path:

    * a `datetime.datetime` (including a `pandas.Timestamp`, which is a
      subclass) is returned as a naive `datetime`;
    * a `datetime.date` is promoted to midnight of that day;
    * a string is parsed with `fmt` when given, falling back to a lenient
      ISO-8601 / pandas parse when `fmt` does not match — so a plain
      `"2022-01-01"` and a full `"2022-01-01T06:00"` both work.

    The result is always **timezone-naive**: a timezone-aware input (an
    offset-bearing string such as `"2022-01-01T06:30:00+02:00"`, or an aware
    `datetime`) is converted to UTC and stripped, so mixing a naive `start`
    with an aware `end` never raises a naive-vs-aware comparison `TypeError`
    downstream.

    Args:
        value: The date-like value — a `datetime`, a `date`, a
            `pandas.Timestamp`, or a string.
        fmt: Optional `strptime` format tried first for string input.
            When `None`, or when it does not match, the string is parsed
            as ISO-8601. Ignored for non-string input.

    Returns:
        The parsed `datetime.datetime`.

    Raises:
        TypeError: If `value` is not a date, datetime, or string.
        ValueError: If a string cannot be parsed by `fmt` or as ISO-8601.

    Examples:
        - An ISO date string is parsed with the default format:
            ```python
            >>> to_datetime("2022-01-01", fmt="%Y-%m-%d")
            datetime.datetime(2022, 1, 1, 0, 0)

            ```
        - A `date` is promoted to midnight:
            ```python
            >>> import datetime as dt
            >>> to_datetime(dt.date(2022, 1, 1))
            datetime.datetime(2022, 1, 1, 0, 0)

            ```
        - A full ISO timestamp string parses even when `fmt` is a plain
          date format that does not match it:
            ```python
            >>> to_datetime("2022-01-01T06:30", fmt="%Y-%m-%d")
            datetime.datetime(2022, 1, 1, 6, 30)

            ```
        - A timezone-aware string is normalized to naive UTC (here `+02:00`
          becomes `04:30`):
            ```python
            >>> to_datetime("2022-01-01T06:30:00+02:00")
            datetime.datetime(2022, 1, 1, 4, 30)

            ```
    """
    if isinstance(value, dt.datetime):
        return _strip_tz(value)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        if fmt is not None:
            try:
                return _strip_tz(dt.datetime.strptime(value, fmt))
            except ValueError:
                pass
        return _strip_tz(pd.Timestamp(value).to_pydatetime())
    raise TypeError(
        f"start / end must be a datetime, date, or string; got "
        f"{type(value).__name__}"
    )


def date_windows(
    start: Any,
    end: Any,
    freq: str,
    *,
    inclusive: str = "both",
) -> pd.DatetimeIndex:
    """Expand a `[start, end]` range into its period starts at `freq`.

    The single home for the `pd.date_range(to_datetime(start),
    to_datetime(end), freq=...)` idiom that ~20 backends re-derive to turn a
    request window into the per-file dates they download. `start` / `end` are
    parsed through :func:`to_datetime`, so either raw date-likes (a string, a
    `date`, a `Timestamp`) or already-parsed `datetime`s work.

    Args:
        start: The window start, in any form :func:`to_datetime` accepts.
        end: The window end, in any form :func:`to_datetime` accepts.
        freq: A pandas offset alias (`"D"`, `"MS"`, `"YS"`, `"6h"`, ...).
        inclusive: Which endpoints to include — `"both"` (default), `"left"`,
            `"right"`, or `"neither"`; forwarded to `pandas.date_range`.

    Returns:
        pandas.DatetimeIndex: One timestamp per period start in the window.

    Examples:
        - A monthly range is expanded to month starts:
            ```python
            >>> [d.strftime("%Y-%m-%d") for d in date_windows(
            ...     "2020-01-01", "2020-03-01", "MS")]
            ['2020-01-01', '2020-02-01', '2020-03-01']

            ```
        - `inclusive="left"` drops the closing endpoint:
            ```python
            >>> [d.strftime("%Y-%m-%d") for d in date_windows(
            ...     "2020-01-01", "2020-03-01", "MS", inclusive="left")]
            ['2020-01-01', '2020-02-01']

            ```
    """
    return pd.date_range(
        to_datetime(start), to_datetime(end), freq=freq, inclusive=inclusive
    )


def window_labels(times: Any, freq: str, *, fmt: str = "%Y%m%d") -> list[str]:
    """Bucket `times` by `freq`; return one window-start label per input time.

    Times that fall in the same `freq` window get the same `strftime(fmt)`
    label, so a downstream `NetCDF.reduce(groupby=...)` /
    `DatasetCollection.groupby(...)` coarsens the time axis to one slice per
    distinct window. The single home for the `pd.Grouper(freq=...)` bucketing
    the cmems / stac / nwp aggregation paths each re-derived.

    Args:
        times: The per-step times, in file order (anything `pandas.to_datetime`
            accepts — a `DatetimeIndex`, a list of `datetime`s / strings, ...).
        freq: A pandas offset alias (`"6h"`, `"D"`, `"1MS"`, `"YS"`, ...).
        fmt: `strftime` format for the label. Defaults to `"%Y%m%d"`; sub-daily
            windows should pass `"%Y%m%d%H"` so same-day windows stay distinct.

    Returns:
        list[str]: One label per input time (same length / order as `times`).

    Examples:
        - Monthly windows collapse the days in each month to one label:
            ```python
            >>> window_labels(
            ...     ["2020-01-05", "2020-01-20", "2020-02-03"], "MS")
            ['20200101', '20200101', '20200201']

            ```
    """
    import pandas as pd

    index = pd.DatetimeIndex(pd.to_datetime(list(times)))
    positions = pd.Series(range(len(index)), index=index)
    label_for: dict[int, str] = {}
    for window_start, group in positions.groupby(pd.Grouper(freq=freq)):
        if group.empty:
            continue
        label = window_start.strftime(fmt)
        for pos in group.tolist():
            label_for[int(pos)] = label
    return [label_for[i] for i in range(len(index))]


def split_time(value: Any) -> tuple[Any, Any]:
    """Split a single time-range value into a `(start, end)` pair.

    The ergonomic alternative to passing `start` / `end` separately, in the
    spirit of STAC's `datetime="a/b"` and earthaccess's `temporal=(a, b)`.
    The two halves keep their original types and are parsed downstream by
    :func:`to_datetime`. Accepted forms:

    * a `"start/end"` string (STAC interval) — split on the first `/`; an
      empty half (`"a/"` / `"/b"`) becomes `None` (open-ended);
    * a `(start, end)` / `[start, end]` 2-sequence;
    * a `slice(start, stop)` — `value.start` / `value.stop` (a `step` is
      rejected, since a step has no meaning on a date range);
    * a single date-like string / `datetime` / `date` — an instant, returned
      as `(value, value)`.

    Args:
        value: The time range in any of the accepted forms.

    Returns:
        `(start, end)`, each a date-like value (or `None` for an open half).

    Raises:
        ValueError: If a sequence does not have exactly two elements, or if
            a `slice` carries a `step`.
        TypeError: If `value` is of an unsupported type.

    Examples:
        - A STAC-style interval string splits on `/`:
            ```python
            >>> split_time("2020-01-01/2020-01-31")
            ('2020-01-01', '2020-01-31')

            ```
        - A two-tuple is returned verbatim:
            ```python
            >>> split_time(("2020-01-01", "2020-02-01"))
            ('2020-01-01', '2020-02-01')

            ```
        - A single date is an instant (start == end):
            ```python
            >>> split_time("2020-01-01")
            ('2020-01-01', '2020-01-01')

            ```
    """
    if isinstance(value, slice):
        if value.step is not None:
            raise ValueError(
                f"time= slice does not accept a step; got step={value.step!r}"
            )
        return value.start, value.stop
    if isinstance(value, str):
        if "/" in value:
            start, _, end = value.partition("/")
            return (start.strip() or None), (end.strip() or None)
        return value, value
    if isinstance(value, (dt.datetime, dt.date)):
        return value, value
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError(
                f"time= sequence must have 2 elements [start, end]; "
                f"got {len(value)}"
            )
        return value[0], value[1]
    raise TypeError(
        "time= must be a 'start/end' string, a (start, end) pair, a slice, "
        f"or a single date; got {type(value).__name__}"
    )
