"""Long-term climatology and anomalies over an Earth Engine collection.

A climatology is the reducer (mean by default) *per calendar period across many
years* — the 1991-2020 June mean, the day-of-year normal — which is a different
grouping from `CadenceBucketer`'s consecutive buckets: `Cadence` splits one
window into successive periods, a climatology collapses the *same* calendar
period over every year in a baseline. It is the semantics of xarray's
`groupby("time.dayofyear")`, and the half an anomaly needs ("this June against
the 1991-2020 June mean").

`Climatology` groups by `dayofyear`, `month`, or `season`, reduces each group
through the Epic-1 `Reducer` registry (so there is no fourth reducer
vocabulary), and carries an explicit `baseline` of reference years so an anomaly
has a declared baseline rather than an implicit one. `climatology()` returns one
image per period; `anomaly()` is the companion — the observed period minus its
baseline climatology, in one call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

import ee
from pydantic import BaseModel, ConfigDict, Field, field_validator

from earthlens.gee.reducers import TemporalReducer, temporal_reducer_for

__all__ = ["Climatology"]

#: The three-month groups each named season expands to. December belongs to the
#: following year's winter in meteorological convention, but a climatology
#: collapses across years, so the wrap does not matter here.
_SEASONS: Mapping[str, tuple[int, ...]] = {
    "DJF": (12, 1, 2),
    "MAM": (3, 4, 5),
    "JJA": (6, 7, 8),
    "SON": (9, 10, 11),
}

#: The Earth Engine `calendarRange` unit for each single-value period type.
_CALENDAR_UNIT: Mapping[str, str] = {"dayofyear": "day_of_year", "month": "month"}


class Climatology(BaseModel):
    """A per-period climatology over a baseline, with an anomaly companion.

    Groups a collection by `period` across the `baseline` years and reduces each
    group with `reducer`. The reducer is resolved through the Epic-1
    `temporal_reducer_for` registry, so a string (`"mean"`, `"p90"`) or a
    `TemporalReducer` instance both work and no new reducer vocabulary is
    introduced.

    Attributes:
        period: The calendar grouping — `"dayofyear"` (1-366), `"month"`
            (1-12), or `"season"` (`DJF` / `MAM` / `JJA` / `SON`).
        reducer: The reducer applied within each period group. Defaults to the
            mean; accepts any `temporal_reducer_for` spelling or instance.
        baseline: The inclusive `(start_year, end_year)` reference window the
            climatology is computed over, so an anomaly's baseline is explicit.

    Examples:
        - The grouping and an explicit baseline, with the default reducer:
            ```python
            >>> clim = Climatology(period="month", baseline=(1991, 2020))
            >>> clim.reducer.name
            'mean'
            >>> clim.periods()[:3]
            [1, 2, 3]
            >>> len(Climatology(period="dayofyear", baseline=(1991, 2020)).periods())
            366
            >>> Climatology(period="season", baseline=(1991, 2020)).periods()
            ['DJF', 'MAM', 'JJA', 'SON']

            ```
        - A reversed baseline is refused at construction:
            ```python
            >>> Climatology(period="month", baseline=(2020, 1991))
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for Climatology
            ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    period: Literal["dayofyear", "month", "season"]
    reducer: TemporalReducer = Field(
        default_factory=lambda: temporal_reducer_for("mean")
    )
    baseline: tuple[int, int]

    #: The inclusive upper bound each period type accepts, validated before a
    #: period value is turned into a filter.
    _PERIOD_MAX: ClassVar[Mapping[str, int]] = {"dayofyear": 366, "month": 12}

    @field_validator("reducer", mode="before")
    @classmethod
    def _coerce_reducer(cls, value: TemporalReducer | str) -> TemporalReducer:
        """Resolve a reducer name or instance through the Epic-1 registry.

        Args:
            value: A `TemporalReducer`, a registry name (`"mean"`), or a
                percentile spec / shorthand (`"percentile:90"`, `"p90"`).

        Returns:
            The resolved `TemporalReducer`.

        Raises:
            ValueError: If `value` names a region-only or unknown reducer.
        """
        return temporal_reducer_for(value)

    @field_validator("baseline")
    @classmethod
    def _check_baseline(cls, value: tuple[int, int]) -> tuple[int, int]:
        """Reject a baseline whose start year is after its end year.

        Args:
            value: The `(start_year, end_year)` pair.

        Returns:
            The baseline unchanged once it is ordered.

        Raises:
            ValueError: If `start_year > end_year`.
        """
        start, end = value
        if start > end:
            raise ValueError(
                f"baseline start year {start} is after end year {end}; "
                "pass (start_year, end_year)"
            )
        return value

    def periods(self) -> list[int] | list[str]:
        """Return the period values this climatology groups by.

        Returns:
            `1`-`366` for day-of-year, `1`-`12` for month, or the four season
            names.
        """
        if self.period == "month":
            values: list[int] | list[str] = list(range(1, 13))
        elif self.period == "dayofyear":
            values = list(range(1, 367))
        else:
            values = list(_SEASONS)
        return values

    def _validate_period_value(self, period_value: int | str) -> None:
        """Raise if `period_value` is not valid for this climatology's `period`.

        Args:
            period_value: A month/day-of-year integer, or a season name.

        Raises:
            ValueError: If the value is out of range, or an unknown season.
        """
        if self.period == "season":
            if period_value not in _SEASONS:
                raise ValueError(
                    f"{period_value!r} is not a season; use one of {list(_SEASONS)}"
                )
        elif not (
            isinstance(period_value, int)
            and 1 <= period_value <= self._PERIOD_MAX[self.period]
        ):
            raise ValueError(
                f"{period_value!r} is not a valid {self.period}; expected "
                f"1-{self._PERIOD_MAX[self.period]}"
            )

    def _period_filter(self, period_value: int | str) -> Any:
        """Build the Earth Engine filter selecting one period across all years.

        Args:
            period_value: A month/day-of-year integer, or a season name.

        Returns:
            An `ee.Filter` matching every scene in that calendar period. A
            season expands to an `Or` over its three months.
        """
        if self.period == "season":
            months = _SEASONS[str(period_value)]
            selected = ee.Filter.Or(
                *[ee.Filter.calendarRange(month, month, "month") for month in months]
            )
        else:
            unit = _CALENDAR_UNIT[self.period]
            selected = ee.Filter.calendarRange(period_value, period_value, unit)
        return selected

    def _in_baseline(self, collection: Any) -> Any:
        """Restrict a collection to the baseline years.

        Args:
            collection: The input `ee.ImageCollection`.

        Returns:
            The collection filtered to `[start_year, end_year]`.
        """
        start, end = self.baseline
        return collection.filter(ee.Filter.calendarRange(start, end, "year"))

    def _reduce_period(self, collection: Any, period_value: int | str) -> Any:
        """Reduce one period's scenes to a single labelled image.

        Args:
            collection: The collection to filter and reduce.
            period_value: The period to keep before reducing.

        Returns:
            The reduced `ee.Image`, stamped with a `period` property.
        """
        scenes = collection.filter(self._period_filter(period_value))
        reduced = self.reducer.apply_ee(scenes)
        return ee.Image(reduced).set("period", period_value)

    def climatology(self, collection: Any) -> Any:
        """Reduce the baseline collection into one image per period.

        Args:
            collection: The input `ee.ImageCollection`; only its baseline years
                contribute.

        Returns:
            An `ee.ImageCollection` of one reduced image per period, each
            carrying its `period` property.
        """
        baseline = self._in_baseline(collection)
        images = [self._reduce_period(baseline, value) for value in self.periods()]
        return ee.ImageCollection(images)

    def anomaly(
        self,
        baseline_collection: Any,
        observed_collection: Any,
        period_value: int | str,
    ) -> Any:
        """Return the anomaly of one observed period against its climatology.

        The baseline climatology for `period_value` is reduced from the baseline
        years of `baseline_collection`; the observed value is reduced from the
        same period of `observed_collection`; the anomaly is their difference —
        the "this June against the 1991-2020 June mean" computation, in one call.

        Args:
            baseline_collection: The collection the climatology is drawn from;
                only its baseline years contribute.
            observed_collection: The collection the observed period is drawn
                from (e.g. a single year to compare against the baseline).
            period_value: The period to compare — a month/day-of-year integer or
                a season name, validated against this climatology's `period`.

        Returns:
            The anomaly `ee.Image` (observed minus baseline), stamped with its
            `period`.

        Raises:
            ValueError: If `period_value` is not valid for this `period`.
        """
        self._validate_period_value(period_value)
        baseline = self._reduce_period(
            self._in_baseline(baseline_collection), period_value
        )
        observed = self._reduce_period(observed_collection, period_value)
        return ee.Image(observed.subtract(baseline).set("period", period_value))
