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


def to_datetime(value: Any, fmt: str | None = None) -> dt.datetime:
    """Coerce a date-like value into a `datetime.datetime`.

    Accepts the rich set of inputs the popular EO packages accept, while
    staying backward compatible with the legacy `strptime`-with-`fmt`
    string path:

    * a `datetime.datetime` (including a `pandas.Timestamp`, which is a
      subclass) is returned unchanged;
    * a `datetime.date` is promoted to midnight of that day;
    * a string is parsed with `fmt` when given, falling back to a lenient
      ISO-8601 / pandas parse when `fmt` does not match — so a plain
      `"2022-01-01"` and a full `"2022-01-01T06:00"` both work.

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
    """
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        if fmt is not None:
            try:
                return dt.datetime.strptime(value, fmt)
            except ValueError:
                pass
        import pandas as pd

        return pd.Timestamp(value).to_pydatetime()
    raise TypeError(
        f"start / end must be a datetime, date, or string; got "
        f"{type(value).__name__}"
    )
