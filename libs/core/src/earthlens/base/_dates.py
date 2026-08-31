"""Flexible parsing of user-supplied start / end dates.

Backends historically required `start` / `end` as strings parsed with an
explicit `strptime` `fmt`. This helper lets the same call sites accept a
`datetime`, a `date`, a `pandas.Timestamp`, or a string — and parse the
string as ISO-8601 when the caller's `fmt` does not match — so the `fmt`
parameter becomes an optional override rather than a requirement.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections.abc import Mapping
from typing import Any

import pandas as pd

#: Matches an ISO date/time separator anchored between two digits, so a
#: month name containing a "t" is not mistaken for one.
_ISO_T_SEP = re.compile(r"\d[Tt]\d")


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
        return value.astimezone(dt.UTC).replace(tzinfo=None)
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
        f"start / end must be a datetime, date, or string; got {type(value).__name__}"
    )


#: Sentinel alias meaning "no fixed spacing — query the window whole". Returned
#: by :func:`resolve_cadence` for any cadence that names a release *character*
#: rather than a period, and so has no pandas offset: `"irregular"`,
#: `"climatology"`, `"subhourly"`, `"subdaily"`, `"raw"`, `"native"` and
#: `"static"`. `AbstractDataSource._cadence_extent` turns it into a whole-window
#: extent instead of trying to expand a period axis.
WHOLE_WINDOW = "all"

#: The cadence vocabulary the providers' own catalogs use, mapped to pandas
#: offset aliases. Shared rather than re-spelled per backend: the narrow
#: three-entry maps the backends each carried rejected cadences their catalogs
#: legitimately name - many of CMEMS's 1141 rows say `irregular` / `annual` /
#: `climatology` / `weekly` / `6hourly`. A backend that genuinely supports only
#: a subset passes its own narrower map.
CADENCE_ALIASES: dict[str, str] = {
    # Sub-hourly (eumetsat's rapid-scan and full-disc SEVIRI / MTG rows).
    "5min": "5min",
    "10min": "10min",
    "15min": "15min",
    "30min": "30min",
    # Hourly multiples.
    "hourly": "h",
    "3hourly": "3h",
    "6hourly": "6h",
    "12hourly": "12h",
    # Daily multiples, including the MODIS/VIIRS composite periods
    # (earthdata's `8day` / `16day`) and the dekad (eumetsat, drought).
    # The multi-day cadences are deliberately **sliding from the window start**
    # (`5D` / `7D` / `8D` / `10D` / `16D`) rather than calendar-anchored. Two
    # reasons: `date_windows` promises one timestamp per period *start*, and
    # pandas' calendar-anchored weekly alias `W` is `W-SUN`, i.e. a period *end*
    # — `date_range("2024-02-01", ..., freq="W")` begins on the 4th and never
    # emits the 1st, so a download loop over the axis would silently skip the
    # first days of the requested window. The sliding forms always start exactly
    # at the window start and tile it completely, which is what a per-period
    # fetch loop needs. MODIS/VIIRS `8day` / `16day` composites are themselves
    # sliding from a yearly epoch, so this also matches the products.
    "daily": "D",
    "pentadal": "5D",
    "weekly": "7D",
    "8day": "8D",
    "10day": "10D",
    "dekadal": "10D",
    "16day": "16D",
    "monthly": "MS",
    # Meteorological seasons (DJF / MAM / JJA / SON), the geoscience convention;
    # plain `QS` would anchor on the calendar quarter (Jan / Apr / Jul / Oct).
    "seasonal": "QS-DEC",
    "annual": "YS",
    "yearly": "YS",
    # No fixed period — the window is queried whole. `raw` / `native` mean "as
    # the provider stores it, no temporal aggregation"; `subhourly` / `subdaily`
    # name a release *character* rather than a period, as do `irregular` and
    # `climatology`.
    "raw": WHOLE_WINDOW,
    "native": WHOLE_WINDOW,
    "subhourly": WHOLE_WINDOW,
    "subdaily": WHOLE_WINDOW,
    "irregular": WHOLE_WINDOW,
    "climatology": WHOLE_WINDOW,
    "static": WHOLE_WINDOW,
    "all": WHOLE_WINDOW,
}


def resolve_cadence(
    cadence: str,
    accepted: Mapping[str, str],
    *,
    backend: str = "this backend",
) -> str:
    """Map a user-facing cadence onto its pandas offset alias, or raise.

    The single home for the cadence lookup the multi-cadence backends each
    spelled as `freq_map.get(temporal_resolution, "D")` — a form that silently
    substitutes a *different* cadence when the caller's spelling is unknown, so
    a mistyped `cadence="dailyy"` (or a legitimate `"yearly"` missing from a
    backend's map) quietly downloaded daily steps instead of failing. Raising
    is the only safe behaviour: the alternative silently changes both the
    request count and the output shape.

    Args:
        cadence: The user-facing cadence (`temporal_resolution` / `cadence=`),
            e.g. `"daily"`.
        accepted: Mapping of every cadence this backend supports to its pandas
            offset alias, e.g. `{"daily": "D", "monthly": "MS"}`.
        backend: Backend name used in the error message. Defaults to a generic
            phrase; pass `type(self).__name__`.

    Returns:
        The pandas offset alias for `cadence`.

    Raises:
        ValueError: If `cadence` is not a key of `accepted`. The message lists
            the accepted spellings and suggests the closest one.

    Examples:
        - A supported cadence resolves to its pandas alias:
            ```python
            >>> from earthlens.base import resolve_cadence
            >>> resolve_cadence("monthly", {"daily": "D", "monthly": "MS"})
            'MS'

            ```
        - An unsupported cadence raises instead of defaulting:
            ```python
            >>> from earthlens.base import resolve_cadence
            >>> resolve_cadence(  # doctest: +ELLIPSIS
            ...     "yearly", {"daily": "D", "monthly": "MS"}
            ... )
            Traceback (most recent call last):
                ...
            ValueError: temporal_resolution='yearly' is not supported by this backend. ...

            ```
        - A near-miss gets a did-you-mean hint:
            ```python
            >>> from earthlens.base import resolve_cadence
            >>> try:
            ...     resolve_cadence("dailyy", {"daily": "D"}, backend="CMEMS")
            ... except ValueError as exc:
            ...     print("Did you mean 'daily'?" in str(exc))
            True

            ```
    """
    if not isinstance(cadence, str):
        # Guard before difflib, which raises a bare `TypeError: 'NoneType'
        # object is not iterable` on a non-string — the very failure shape this
        # function exists to replace.
        raise ValueError(
            f"temporal_resolution must be a string cadence, got "
            f"{type(cadence).__name__}. Accepted by {backend}: {sorted(accepted)}."
        )
    try:
        return accepted[cadence]
    except KeyError:
        close = difflib.get_close_matches(cadence, accepted, n=1)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        raise ValueError(
            f"temporal_resolution={cadence!r} is not supported by {backend}. "
            f"Accepted: {sorted(accepted)}.{hint}"
        ) from None


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
                f"time= sequence must have 2 elements [start, end]; got {len(value)}"
            )
        return value[0], value[1]
    raise TypeError(
        "time= must be a 'start/end' string, a (start, end) pair, a slice, "
        f"or a single date; got {type(value).__name__}"
    )


def end_is_date_only(end: str | dt.date | dt.datetime) -> bool:
    """Return whether an end bound was given as a bare date (no time-of-day).

    The decision keys off the **input**, not the parsed value, so an
    end the user typed with an explicit midnight time (`"2026-07-04 00:00"`)
    is *not* treated as a bare date. A `datetime.date` (but not a
    `datetime.datetime`) is bare; a string is bare when it carries no time
    separator — a `:` (any `HH:MM`) or an ISO `T`/`t` *between two digits*
    (`2026-07-03T00`). The digit-anchored `T` check avoids false-positives
    on month-name formats whose name contains a `t` (`Oct` / `September`).

    Args:
        end: The raw end bound as passed to the backend (string, `date`,
            or `datetime`).

    Returns:
        bool: `True` when `end` denotes a whole calendar day.

    Examples:
        - A bare date string is date-only; a timed one is not:
            ```python
            >>> from earthlens.base import end_is_date_only
            >>> end_is_date_only("2026-07-03")
            True
            >>> end_is_date_only("2026-07-04 00:00")
            False

            ```
    """
    if isinstance(end, dt.datetime):
        return False
    if isinstance(end, dt.date):
        return True
    if isinstance(end, str):
        return ":" not in end and _ISO_T_SEP.search(end) is None
    return False


def expand_bare_date_end(end: dt.datetime, *, date_only: bool) -> dt.datetime:
    """Push a bare-date end bound to the last microsecond of its UTC day.

    A user who passes a bare date with the default `fmt="%Y-%m-%d"` (e.g.
    `end="2026-07-03"`) means "include the whole of 3 July" — but that
    parses to `00:00:00`, and ABI scans never land exactly at midnight, so
    an unexpanded inclusive filter would drop the entire day. When
    `date_only` is `True` the bound is expanded to `23:59:59.999999` of the
    same day; otherwise (an explicit time, including an explicit midnight)
    it is returned untouched. Use :func:`end_is_date_only` on the raw input
    to decide `date_only`.

    Args:
        end: The parsed end bound.
        date_only: Whether the raw input denoted a whole calendar day.

    Returns:
        datetime.datetime: `end` unchanged, or expanded to end-of-day.

    Examples:
        - A bare date expands; an explicit time is left as-is:
            ```python
            >>> import datetime as dt
            >>> from earthlens.base import expand_bare_date_end
            >>> expand_bare_date_end(dt.datetime(2026, 7, 3), date_only=True)
            datetime.datetime(2026, 7, 3, 23, 59, 59, 999999)
            >>> expand_bare_date_end(dt.datetime(2026, 7, 4), date_only=False)
            datetime.datetime(2026, 7, 4, 0, 0)

            ```
    """
    if date_only:
        return end.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end
