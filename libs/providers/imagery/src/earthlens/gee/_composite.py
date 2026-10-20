"""Cadence bucketing and temporal compositing for the GEE backend.

The backend used to bucket a request window with its own three-entry
`_RESOLUTION_FREQ = {"daily": "D", "monthly": "MS", "yearly": "YS"}` and a
`freq_map.get(..., "D")`-style lookup, duplicating the framework's richer
`CADENCE_ALIASES` and silently substituting daily steps for an unknown cadence.

`CadenceBucketer` centralises that mapping on the framework's own
`resolve_cadence`, so an unknown cadence raises a did-you-mean `ValueError`
instead of defaulting, and surfaces the `"raw"` whole-window case as one
`is_whole_window` property rather than a special-case scattered through the
backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pandas as pd
from pydantic import BaseModel, ConfigDict

from earthlens.base import WHOLE_WINDOW, date_windows, resolve_cadence
from earthlens.gee.reducers import TemporalReducer
from earthlens.gee.work import RasterWorkUnit

if TYPE_CHECKING:  # pragma: no cover - typing only
    import datetime as dt
    from collections.abc import Iterator
    from typing import Any

__all__ = ["Bucket", "CadenceBucketer", "CompositeEngine"]

#: The cadences the GEE backend buckets by, mapped to their pandas offset
#: aliases. The single source for the backend's cadence vocabulary — the
#: `_RESOLUTION_FREQ` the backend still exposes is derived from this. `"raw"`
#: maps to `WHOLE_WINDOW` (the whole request window is one bucket); a cadence
#: outside this set is rejected by `resolve_cadence`, not silently defaulted.
_GEE_CADENCES: dict[str, str] = {
    "daily": "D",
    "monthly": "MS",
    "yearly": "YS",
    "raw": WHOLE_WINDOW,
}


class CadenceBucketer:
    """Turns a `temporal_resolution` into a pandas frequency and its windows.

    Resolves the cadence through the framework `resolve_cadence` against the
    GEE-accepted subset, so an unknown spelling raises with a did-you-mean hint
    rather than defaulting to daily. `"raw"` resolves to `WHOLE_WINDOW`, surfaced
    as `is_whole_window`, which the compositing special case keys off.

    Examples:
        - A monthly cadence resolves to its pandas alias and is not whole-window:
            ```python
            >>> bucketer = CadenceBucketer("monthly")
            >>> bucketer.freq
            'MS'
            >>> bucketer.is_whole_window
            False

            ```
        - `"raw"` is the single whole-window bucket:
            ```python
            >>> CadenceBucketer("raw").is_whole_window
            True

            ```
        - An unknown cadence is refused with a suggestion, not defaulted:
            ```python
            >>> CadenceBucketer("monthy")  # doctest: +ELLIPSIS
            Traceback (most recent call last):
                ...
            ValueError: temporal_resolution='monthy' is not supported by GEE. ...

            ```
    """

    def __init__(self, temporal_resolution: str) -> None:
        """Resolve the cadence to its pandas frequency once, at construction.

        Args:
            temporal_resolution: The request cadence — `"raw"`, `"daily"`,
                `"monthly"`, or `"yearly"`.

        Raises:
            ValueError: If `temporal_resolution` is not one the backend buckets
                by; the message names the accepted spellings and the nearest.
        """
        self._cadence = temporal_resolution
        self._freq = resolve_cadence(temporal_resolution, _GEE_CADENCES, backend="GEE")

    @property
    def cadence(self) -> str:
        """Return the request cadence this bucketer was built for."""
        return self._cadence

    @property
    def freq(self) -> str:
        """Return the pandas offset alias, or `WHOLE_WINDOW` for `"raw"`."""
        return self._freq

    @property
    def is_whole_window(self) -> bool:
        """Return whether the window is one bucket (`"raw"`), not a period split."""
        return self._freq == WHOLE_WINDOW

    def windows(self, start: dt.datetime, end: dt.datetime) -> pd.DatetimeIndex:
        """Return the period-start timestamps tiling `[start, end)`.

        Delegates to the framework `date_windows(..., inclusive="left")`, so the
        bucket edges match the backend's pre-refactor split exactly. Call only
        for a periodic cadence — a whole-window bucketer has no period axis.

        Args:
            start: The window start.
            end: The window end.

        Returns:
            One `pandas.Timestamp` per period start, the left edge of each
            bucket.
        """
        return date_windows(start, end, self._freq, inclusive="left")


class Bucket(NamedTuple):
    """One composite bucket's half-open `[start, end)` window.

    Attributes:
        start: The bucket's inclusive start — also the timestamp the output
            filename is derived from.
        end: The bucket's exclusive end.
    """

    start: dt.datetime
    end: dt.datetime


class CompositeEngine(BaseModel):
    """Buckets a request window and reduces each bucket to one image.

    Holds a `TemporalReducer` and a `CadenceBucketer`, and yields exactly the
    `RasterWorkUnit`s the backend's procedural `GEE._composite` used to yield, so
    the `_api` / download seam is unchanged: `GEE._composite` is now a thin shim
    that builds one of these and delegates to `composite_ee`.

    The compositing special case is preserved exactly: `temporal_resolution="raw"`
    or a static (non-collection) dataset is one whole-window bucket reducing the
    whole collection once; otherwise the window is split with
    `date_windows(..., inclusive="left")`, the last bucket's end pinned to the
    request end, and each bucket's scenes filtered by `filterDate` before the
    reducer.

    Attributes:
        reducer: The time-axis reducer applied to each bucket's collection.
        cadence: The bucketer that turns the request window into bucket edges.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    reducer: TemporalReducer
    cadence: CadenceBucketer

    def buckets(
        self, start: dt.datetime, end: dt.datetime, *, is_image_collection: bool
    ) -> Iterator[Bucket]:
        """Yield the half-open windows the request window splits into.

        Args:
            start: The request window's inclusive start.
            end: The request window's exclusive end.
            is_image_collection: Whether the dataset is a collection; a static
                image reduces as one whole-window bucket regardless of cadence.

        Yields:
            One `Bucket` per time bucket — a single `[start, end)` for a
            whole-window request, else one per period with the last end pinned
            to `end`.
        """
        if self.cadence.is_whole_window or not is_image_collection:
            yield Bucket(start, end)
            return
        starts = self.cadence.windows(start, end)
        for i, bucket_start in enumerate(starts):
            bucket_end = starts[i + 1] if i + 1 < len(starts) else pd.Timestamp(end)
            yield Bucket(bucket_start.to_pydatetime(), bucket_end.to_pydatetime())

    def composite_ee(
        self,
        collection: Any,
        start: dt.datetime,
        end: dt.datetime,
        *,
        is_image_collection: bool,
    ) -> Iterator[RasterWorkUnit]:
        """Yield one reduced `RasterWorkUnit` per bucket, server-side.

        A whole-window request reduces the whole collection once; a periodic one
        reduces each bucket's `filterDate` window. The yielded unit carries the
        reduced `ee.Image` and the bucket it covers, matching the four-tuple the
        download seam already consumes.

        Args:
            collection: The filtered `ee.ImageCollection` to composite.
            start: The request window's inclusive start.
            end: The request window's exclusive end.
            is_image_collection: Whether the dataset is a collection.

        Yields:
            One `RasterWorkUnit` per bucket.
        """
        whole = self.cadence.is_whole_window or not is_image_collection
        for bucket in self.buckets(start, end, is_image_collection=is_image_collection):
            source = (
                collection
                if whole
                else collection.filterDate(
                    bucket.start.strftime("%Y-%m-%d"),
                    bucket.end.strftime("%Y-%m-%d"),
                )
            )
            yield RasterWorkUnit(
                self.reducer.apply_ee(source),
                bucket.start,
                bucket.start,
                bucket.end,
            )
