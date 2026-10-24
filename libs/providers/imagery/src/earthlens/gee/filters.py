"""Small `ee.ImageCollection` filter helpers.

Thin wrappers around `ee.ImageCollection.filter*` so the call site
reads as intent rather than as an Earth Engine filter expression.

Filters come in two equivalent forms:

* **Callable classes** under the class-form `CollectionFilter` base —
  `ByYear`, `ByBounds`, `ByPropertyIn`, `ByCloudCoverLte`,
  `ByYearAndBounds`, plus the expanded scene selectors `ByOrbit`,
  `BySensor`, `ByQuality`, `BySeason`, `ByMonth` and `ByDayOfYear` —
  frozen pydantic objects that carry validated config and are drop-ins
  for a `GEE(filters=[...])` entry (the hook only requires a
  callable). A typo'd keyword or an out-of-range value raises at
  construction instead of silently no-op'ing the way a misspelled
  `functools.partial` keyword would.
* **Module functions** — `by_year`, `by_bounds`, `by_property_in`,
  `by_cloud_cover_lte`, `by_year_and_bounds` — the original public
  surface, kept as thin behaviour-identical wrappers that construct
  the class and call it.

Ported (and renamed for the in-`filters` namespace) from
`gee_utils.raster.filter_*`. The `filter_` prefix is dropped: under
`earthlens.gee.filters` the call sites read `filters.by_year(...)`,
`filters.by_bounds(...)`, etc., which is clearer than
`filters.filter_year(...)`.

The expanded selectors (Task 6.5) cover what SAR, multi-sensor and
seasonal work need: `ByOrbit` pins a Sentinel-1 pass direction and/or
relative orbit (mixing look geometries in a composite is almost always
wrong), `BySensor` selects platforms by metadata list, `ByQuality` is
the generic metadata-threshold escape hatch, and `BySeason` / `ByMonth`
/ `ByDayOfYear` select calendar periods *across every year* in the date
window via `ee.Filter.calendarRange` — what a plain `filterDate` cannot
express. `BySeason` expands a named season to an `Or` over its months,
the same idiom `earthlens.gee.climatology` uses, so DJF wraps the year
boundary correctly. These are new, class-only selectors; no function
wrappers exist for them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, cast

import ee
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from earthlens.gee.climatology import _SEASONS

__all__ = [
    "ByBounds",
    "ByCloudCoverLte",
    "ByDayOfYear",
    "ByMonth",
    "ByOrbit",
    "ByPropertyIn",
    "ByQuality",
    "BySeason",
    "BySensor",
    "ByYear",
    "ByYearAndBounds",
    "CollectionFilter",
    "by_bounds",
    "by_cloud_cover_lte",
    "by_property_in",
    "by_year",
    "by_year_and_bounds",
]


class CollectionFilter(BaseModel):
    """Callable collection filter — a frozen drop-in for a `filters=` entry.

    Subclasses carry their config as validated pydantic fields
    (`frozen=True`, `extra="forbid"`) and implement
    `__call__(collection) -> ee.ImageCollection`, so an instance plugs
    into `GEE(filters=[...])` — applied left to right after
    `filterDate` / `filterBounds`, before the `cloud_mask` — with no
    backend change.

    Naming: the top-level `earthlens.gee.CollectionFilter` export is the
    plain `Callable[[ee.ImageCollection], ee.ImageCollection]` *type
    alias* defined in `earthlens.gee.backend`, which keeps describing
    the hook; this class — `earthlens.gee.filters.CollectionFilter` —
    is the frozen-pydantic base whose instances satisfy that alias.

    Setting any filter forces server-side compute: the EEDAI fast path
    declines the request (`EngineConfig.needs_server_side_compute`) and
    it runs on an Earth Engine engine instead.

    Examples:
        - A concrete filter carries its config as readable fields:
            ```python
            >>> ByYear(year=2020).year
            2020

            ```
        - The base class only names the contract; calling it raises:
            ```python
            >>> CollectionFilter()(None)
            Traceback (most recent call last):
                ...
            NotImplementedError: CollectionFilter must implement __call__(collection)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Filter one collection; concrete filters implement this.

        Args:
            collection: The `ee.ImageCollection` to filter.

        Returns:
            The filtered `ee.ImageCollection`.

        Raises:
            NotImplementedError: Always — `CollectionFilter` itself is
                the contract, not a filter.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement __call__(collection)"
        )


class ByYear(CollectionFilter):
    """Filter an `ee.ImageCollection` to a calendar year.

    The class form of `by_year`: equivalent to
    `collection.filterDate(f"{year}-01-01", f"{year + 1}-01-01")` — the
    upper bound is exclusive (Earth Engine convention).

    Attributes:
        year: Four-digit year, e.g. `2024`.

    Examples:
        - The year is carried as validated config:
            ```python
            >>> ByYear(year=2024).year
            2024

            ```
        - An instance is a `filters=` drop-in for the GEE backend:
            ```python
            >>> from earthlens.core import EarthLens  # doctest: +SKIP
            >>> el = EarthLens(  # doctest: +SKIP
            ...     data_source="gee",
            ...     dataset="COPERNICUS/S2_SR_HARMONIZED",
            ...     variables=["B4"], start="2024-01-01", end="2024-12-31",
            ...     filters=[ByYear(year=2024)],
            ... )

            ```
    """

    year: int

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the calendar-year date filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` containing images with
            `system:time_start` inside `[year-01-01, year+1-01-01)`.
        """
        return cast(
            "ee.ImageCollection",
            collection.filterDate(f"{self.year}-01-01", f"{self.year + 1}-01-01"),
        )


class ByBounds(CollectionFilter):
    """Filter an `ee.ImageCollection` to images intersecting a region.

    The class form of `by_bounds` — a thin alias around
    `collection.filterBounds(region)`, kept so the intent ("filter by
    spatial bounds") is named at the call site and composes with the
    other filters.

    Attributes:
        region: An `ee.Geometry` or `ee.FeatureCollection` defining the
            spatial filter.

    Examples:
        - The region is carried as config:
            ```python
            >>> ByBounds(region="aoi").region
            'aoi'

            ```
    """

    region: Any

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the spatial bounds filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` with the bounds filter applied.
        """
        return collection.filterBounds(self.region)


class ByPropertyIn(CollectionFilter):
    """Filter an `ee.ImageCollection` by metadata-property membership.

    The class form of `by_property_in`: equivalent to
    `collection.filter(ee.Filter.inList(property_name, list(values)))`.
    Useful e.g. for restricting to certain `WRS_PATH` / `WRS_ROW`
    combinations or specific scene IDs.

    Attributes:
        property_name: The metadata property name to match.
        values: The acceptable values, stored as a tuple on the frozen
            model (any iterable is accepted and materialised).

    Examples:
        - The values are normalised to a tuple:
            ```python
            >>> ByPropertyIn(property_name="WRS_PATH", values=[176, 177]).values
            (176, 177)

            ```
    """

    property_name: str
    values: tuple[Any, ...]

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the property-membership filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` with the property filter applied.
        """
        return collection.filter(
            ee.Filter.inList(self.property_name, list(self.values))
        )


class ByCloudCoverLte(CollectionFilter):
    """Filter to images whose cloud-cover metadata is `<= max_pct`.

    The class form of `by_cloud_cover_lte`: wraps
    `collection.filter(ee.Filter.lte(property_name, max_pct))`. The
    default `property_name` matches Landsat C2 (`CLOUD_COVER`); pass
    `property_name="CLOUDY_PIXEL_PERCENTAGE"` for Sentinel-2.

    Attributes:
        max_pct: Maximum allowed cloud-cover percentage; validated into
            `[0, 100]` at construction.
        property_name: The metadata property carrying the cloud
            percentage. Defaults to `"CLOUD_COVER"`.

    Examples:
        - The threshold is carried as validated config:
            ```python
            >>> ByCloudCoverLte(max_pct=20).property_name
            'CLOUD_COVER'

            ```
        - An out-of-range percentage is refused at construction:
            ```python
            >>> ByCloudCoverLte(max_pct=150)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ByCloudCoverLte
            ...

            ```
    """

    max_pct: float
    property_name: str = "CLOUD_COVER"

    @field_validator("max_pct")
    @classmethod
    def _in_percent_range(cls, value: float) -> float:
        """Refuse a percentage outside the inclusive `[0, 100]` range.

        Args:
            value: The parsed `max_pct` input.

        Returns:
            The value unchanged when it is in range.

        Raises:
            ValueError: If `value` is outside `[0, 100]`.
        """
        if not 0 <= value <= 100:
            raise ValueError(f"max_pct must be in [0, 100], got {value!r}")
        return value

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the cloud-cover metadata filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` with the cloud-cover filter applied.
        """
        return collection.filter(ee.Filter.lte(self.property_name, self.max_pct))


class ByYearAndBounds(CollectionFilter):
    """Compose the calendar-year and spatial-bounds filters.

    The class form of `by_year_and_bounds`: equivalent to
    `ByBounds(region=...)(ByYear(year=...)(collection))` when a region
    is given, else just the year filter.

    Attributes:
        year: Four-digit calendar year.
        region: Optional spatial filter (an `ee.Geometry` or
            `ee.FeatureCollection`). Defaults to `None` (year only).

    Examples:
        - Omitting the region leaves a year-only filter:
            ```python
            >>> ByYearAndBounds(year=2020).region is None
            True

            ```
    """

    year: int
    region: Any = None

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the year filter, then the bounds filter when a region is set.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` with both filters applied (or just
            the year filter when `region is None`).
        """
        out = ByYear(year=self.year)(collection)
        if self.region is not None:
            out = ByBounds(region=self.region)(out)
        return out


class ByOrbit(CollectionFilter):
    """Filter a SAR collection to one orbit pass and/or relative orbit.

    The selector that unlocks coherent Sentinel-1 analysis: ascending
    and descending passes have different look geometries, so mixing
    them in a composite is almost always wrong. Pins
    `orbitProperties_pass` and/or `relativeOrbitNumber_start` with
    `ee.Filter.eq`; at least one must be set, since with neither the
    filter would select everything.

    Attributes:
        pass_direction: The orbit pass to keep — `"ASCENDING"` or
            `"DESCENDING"` — or `None` to accept both. Defaults to
            `None`.
        relative_orbit: The relative orbit number to keep (Sentinel-1
            numbers them from 1), or `None` to accept all. Defaults to
            `None`.

    Examples:
        - Pin the ascending pass:
            ```python
            >>> ByOrbit(pass_direction="ASCENDING").pass_direction
            'ASCENDING'

            ```
        - With neither constraint the filter is refused at construction:
            ```python
            >>> ByOrbit()
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ByOrbit
            ...

            ```
    """

    pass_direction: Literal["ASCENDING", "DESCENDING"] | None = None
    relative_orbit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _at_least_one_constraint(self) -> ByOrbit:
        """Refuse a constraint-free orbit filter.

        Returns:
            The model unchanged when at least one constraint is set.

        Raises:
            ValueError: If both `pass_direction` and `relative_orbit`
                are `None` — the filter would select every scene.
        """
        if self.pass_direction is None and self.relative_orbit is None:
            raise ValueError(
                "ByOrbit needs pass_direction and/or relative_orbit; with "
                "neither it would filter nothing"
            )
        return self

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the configured orbit constraints, pass first.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` keeping only the configured pass
            direction and/or relative orbit.
        """
        out = collection
        if self.pass_direction is not None:
            out = out.filter(ee.Filter.eq("orbitProperties_pass", self.pass_direction))
        if self.relative_orbit is not None:
            out = out.filter(
                ee.Filter.eq("relativeOrbitNumber_start", self.relative_orbit)
            )
        return out


class BySensor(CollectionFilter):
    """Filter a multi-sensor collection to the named platforms.

    Wraps `ee.Filter.inList` over a platform metadata property, so a
    merged or multi-sensor collection can be narrowed to specific
    spacecraft. The default property matches Landsat C2
    (`SPACECRAFT_ID`, e.g. `"LANDSAT_8"`); Sentinel-2 callers pass
    `property_name="SPACECRAFT_NAME"` (`"Sentinel-2A"` / `"-2B"`).

    Attributes:
        values: The acceptable platform names; at least one, stored as
            a tuple on the frozen model.
        property_name: The metadata property carrying the platform
            name. Defaults to `"SPACECRAFT_ID"`.

    Examples:
        - Keep only Landsat 8 and 9 scenes:
            ```python
            >>> BySensor(values=("LANDSAT_8", "LANDSAT_9")).values
            ('LANDSAT_8', 'LANDSAT_9')

            ```
    """

    values: tuple[str, ...] = Field(min_length=1)
    property_name: str = "SPACECRAFT_ID"

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the platform-membership filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` keeping only the named platforms.
        """
        return collection.filter(
            ee.Filter.inList(self.property_name, list(self.values))
        )


class ByQuality(CollectionFilter):
    """Filter by a generic metadata threshold — the escape hatch.

    One `property_name` / `op` / `value` triple dispatched to the
    matching `ee.Filter.{lte,gte,lt,gt,eq}` factory, so a new metadata
    threshold does not need a bespoke filter class. For the common
    cloud-cover case prefer `ByCloudCoverLte`, which names the intent.

    `value` is a number for the four ordering comparisons and either a
    number or a string for `eq` — a string metadata property is the
    common case for equality (`SPACECRAFT_ID == "LANDSAT_8"`), and the
    escape hatch would not be one if it could not express it. A string
    with an ordering op is refused at construction: Earth Engine's
    `lte` / `gte` / `lt` / `gt` compare numbers. For equality against
    *several* values use `ByPropertyIn`, and for the Landsat/Sentinel
    platform names `BySensor`.

    Attributes:
        property_name: The metadata property to threshold.
        op: The comparison — one of `"lte"`, `"gte"`, `"lt"`, `"gt"`,
            `"eq"`; anything else is refused at construction.
        value: The threshold value — a number, or a string with
            `op="eq"`. A numeric-looking string stays a string, so the
            comparison matches the property's own type.

    Examples:
        - Keep scenes with a low mean solar zenith angle:
            ```python
            >>> f = ByQuality(property_name="MEAN_SOLAR_ZENITH_ANGLE", op="lte", value=70)
            >>> f.op, f.value
            ('lte', 70.0)

            ```
        - Keep one platform by its string property:
            ```python
            >>> ByQuality(property_name="SPACECRAFT_ID", op="eq", value="LANDSAT_8").value
            'LANDSAT_8'

            ```
        - A string with an ordering comparison is refused:
            ```python
            >>> ByQuality(property_name="SPACECRAFT_ID", op="lte", value="LANDSAT_8")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ByQuality
            ...

            ```
    """

    property_name: str
    op: Literal["lte", "gte", "lt", "gt", "eq"]
    # `float | str` in that order, so a number stays a number; pydantic's
    # smart union leaves a `str` input as a `str` rather than parsing it, which
    # is what keeps `eq` matching the property's own type.
    value: float | str

    @model_validator(mode="after")
    def _comparable(self) -> ByQuality:
        """Refuse a string value for a comparison Earth Engine runs on numbers.

        Returns:
            The filter unchanged when the value suits the operator.

        Raises:
            ValueError: If `value` is a string and `op` is an ordering
                comparison rather than `"eq"`.
        """
        if isinstance(self.value, str) and self.op != "eq":
            raise ValueError(
                f"op={self.op!r} is an ordering comparison, which Earth "
                f"Engine runs on numeric properties; value={self.value!r} is "
                "a string, so only op='eq' can express it (or use "
                "ByPropertyIn / BySensor for a set of names)"
            )
        return self

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the metadata-threshold filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` with the threshold filter applied.
        """
        return collection.filter(
            getattr(ee.Filter, self.op)(self.property_name, self.value)
        )


class BySeason(CollectionFilter):
    """Filter to one meteorological season across every year in the window.

    Selects a named three-month season — `DJF` / `MAM` / `JJA` / `SON`
    — as an `ee.Filter.Or` over its months, each a
    `calendarRange(month, month, "month")`. This is the same
    season-to-months expansion `earthlens.gee.climatology` uses, so DJF
    wraps the year boundary correctly, and it selects the season in
    *every* year of the date window — what seasonal cloud-free
    compositing needs and a plain `filterDate` cannot express.

    Attributes:
        season: The season to keep — `"DJF"`, `"MAM"`, `"JJA"`, or
            `"SON"`.

    Examples:
        - Summer-only scenes across all years:
            ```python
            >>> BySeason(season="JJA").season
            'JJA'

            ```
        - An unknown season name is refused at construction:
            ```python
            >>> BySeason(season="WINTER")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for BySeason
            ...

            ```
    """

    season: Literal["DJF", "MAM", "JJA", "SON"]

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the season filter, an `Or` over the season's months.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` keeping only scenes whose
            `system:time_start` falls in the season, in any year.
        """
        selected = ee.Filter.Or(
            *[
                ee.Filter.calendarRange(month, month, "month")
                for month in _SEASONS[self.season]
            ]
        )
        return collection.filter(selected)


class ByMonth(CollectionFilter):
    """Filter to a calendar-month window across every year in the window.

    Wraps `ee.Filter.calendarRange(start_month, end_month, "month")`:
    one month when `end_month` is omitted, an inclusive window
    otherwise. A start after the end wraps across the year boundary
    (Earth Engine's `calendarRange` semantics), so
    `ByMonth(start_month=11, end_month=2)` selects November through
    February — verified live: `calendarRange(11, 2, "month")` keeps
    months {1, 2, 11, 12}. The keywords are not optional: a pydantic
    model takes no positional arguments. For the four named seasons
    prefer `BySeason`.

    Attributes:
        start_month: First month kept, `1`-`12`.
        end_month: Last month kept (inclusive), `1`-`12`, or `None`
            (the default) to keep exactly `start_month`.

    Examples:
        - A single month:
            ```python
            >>> ByMonth(start_month=6).end_month is None
            True

            ```
        - An out-of-range month is refused at construction:
            ```python
            >>> ByMonth(start_month=13)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: 1 validation error for ByMonth
            ...

            ```
    """

    start_month: int = Field(ge=1, le=12)
    end_month: int | None = Field(default=None, ge=1, le=12)

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the month calendar filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` keeping only scenes inside the
            month window, in any year.
        """
        end = self.end_month if self.end_month is not None else self.start_month
        return collection.filter(
            ee.Filter.calendarRange(self.start_month, end, "month")
        )


class ByDayOfYear(CollectionFilter):
    """Filter to a day-of-year window across every year in the window.

    Wraps `ee.Filter.calendarRange(start_day, end_day, "day_of_year")`:
    one day when `end_day` is omitted, an inclusive window otherwise —
    the finer-grained counterpart to `ByMonth` for phenology-style
    selections.

    Attributes:
        start_day: First day of year kept, `1`-`366`.
        end_day: Last day kept (inclusive), `1`-`366`, or `None` (the
            default) to keep exactly `start_day`.

    Examples:
        - The meteorological summer, in days of year:
            ```python
            >>> f = ByDayOfYear(start_day=152, end_day=243)
            >>> f.start_day, f.end_day
            (152, 243)

            ```
    """

    start_day: int = Field(ge=1, le=366)
    end_day: int | None = Field(default=None, ge=1, le=366)

    def __call__(self, collection: ee.ImageCollection) -> ee.ImageCollection:
        """Apply the day-of-year calendar filter.

        Args:
            collection: The Earth Engine `ImageCollection`.

        Returns:
            A new `ImageCollection` keeping only scenes inside the
            day-of-year window, in any year.
        """
        end = self.end_day if self.end_day is not None else self.start_day
        return collection.filter(
            ee.Filter.calendarRange(self.start_day, end, "day_of_year")
        )


def by_year(collection: ee.ImageCollection, year: int) -> ee.ImageCollection:
    """Filter an `ee.ImageCollection` to a calendar year.

    The function form of `ByYear`: equivalent to
    `collection.filterDate(f"{year}-01-01", f"{year + 1}-01-01")` — the
    upper bound is exclusive (Earth Engine convention).

    Args:
        collection: The Earth Engine `ImageCollection`.
        year: Four-digit year, e.g. `2024`.

    Returns:
        A new `ImageCollection` containing images with
        `system:time_start` inside `[year-01-01, year+1-01-01)`.
    """
    return ByYear(year=year)(collection)


def by_bounds(
    collection: ee.ImageCollection,
    region: ee.Geometry | ee.FeatureCollection,
) -> ee.ImageCollection:
    """Filter an `ee.ImageCollection` to images intersecting `region`.

    The function form of `ByBounds` — a thin alias around
    `collection.filterBounds(region)`, kept as a standalone helper so it
    sits next to the other filters and so the intent ("filter by
    spatial bounds") is named at the call site.

    Args:
        collection: The Earth Engine `ImageCollection`.
        region: An `ee.Geometry` or `ee.FeatureCollection` defining the
            spatial filter.

    Returns:
        A new `ImageCollection` with the bounds filter applied.
    """
    return ByBounds(region=region)(collection)


def by_property_in(
    collection: ee.ImageCollection,
    property_name: str,
    values: Iterable[Any],
) -> ee.ImageCollection:
    """Filter an `ee.ImageCollection` to images whose `property_name` is in `values`.

    The function form of `ByPropertyIn`: equivalent to
    `collection.filter(ee.Filter.inList(property_name, list(values)))`.
    Useful e.g. for restricting to certain `WRS_PATH` / `WRS_ROW`
    combinations or specific scene IDs.

    Args:
        collection: The Earth Engine `ImageCollection`.
        property_name: The metadata property name to match.
        values: Iterable of acceptable values; materialised for
            `ee.Filter.inList`.

    Returns:
        A new `ImageCollection` with the property filter applied.
    """
    return ByPropertyIn(property_name=property_name, values=tuple(values))(collection)


def by_cloud_cover_lte(
    collection: ee.ImageCollection,
    max_pct: float,
    *,
    property_name: str = "CLOUD_COVER",
) -> ee.ImageCollection:
    """Filter to images whose cloud-cover metadata is `<= max_pct`.

    The function form of `ByCloudCoverLte`: wraps
    `collection.filter(ee.Filter.lte(property_name, max_pct))`.
    The default `property_name` matches Landsat C2 (`CLOUD_COVER`);
    pass `property_name="CLOUDY_PIXEL_PERCENTAGE"` for Sentinel-2.

    Args:
        collection: The Earth Engine `ImageCollection`.
        max_pct: Maximum allowed cloud-cover percentage (0-100).
        property_name: The metadata property carrying the cloud
            percentage. Defaults to `"CLOUD_COVER"`.

    Returns:
        A new `ImageCollection` with the cloud-cover filter applied.

    Raises:
        ValueError: If `max_pct` is outside `[0, 100]`.
    """
    return ByCloudCoverLte(max_pct=max_pct, property_name=property_name)(collection)


def by_year_and_bounds(
    collection: ee.ImageCollection,
    year: int,
    region: ee.Geometry | ee.FeatureCollection | None = None,
) -> ee.ImageCollection:
    """Compose `by_year` and `by_bounds`.

    The function form of `ByYearAndBounds`: equivalent to
    `by_bounds(by_year(collection, year), region)` when `region` is
    given, else just `by_year(collection, year)`.

    Args:
        collection: The Earth Engine `ImageCollection`.
        year: Four-digit calendar year.
        region: Optional spatial filter. Defaults to `None` (year only).

    Returns:
        A new `ImageCollection` with both filters applied (or just the
        year filter when `region is None`).
    """
    return ByYearAndBounds(year=year, region=region)(collection)
