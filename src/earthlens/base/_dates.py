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
