"""Small `ee.ImageCollection` filter helpers.

Thin wrappers around `ee.ImageCollection.filter*` so the call site
reads as intent rather than as an Earth Engine filter expression.

Filters come in two equivalent forms:

* **Callable classes** under the class-form `CollectionFilter` base —
  `ByYear`, `ByBounds`, `ByPropertyIn`, `ByCloudCoverLte`,
  `ByYearAndBounds` — frozen pydantic objects that carry validated
  config and are drop-ins for a `GEE(filters=[...])` entry (the hook
  only requires a callable). A typo'd keyword or an out-of-range value
  raises at construction instead of silently no-op'ing the way a
  misspelled `functools.partial` keyword would.
* **Module functions** — `by_year`, `by_bounds`, `by_property_in`,
  `by_cloud_cover_lte`, `by_year_and_bounds` — the original public
  surface, kept as thin behaviour-identical wrappers that construct
  the class and call it.

Ported (and renamed for the in-`filters` namespace) from
`gee_utils.raster.filter_*`. The `filter_` prefix is dropped: under
`earthlens.gee.filters` the call sites read `filters.by_year(...)`,
`filters.by_bounds(...)`, etc., which is clearer than
`filters.filter_year(...)`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

import ee
from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "ByBounds",
    "ByCloudCoverLte",
    "ByPropertyIn",
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
