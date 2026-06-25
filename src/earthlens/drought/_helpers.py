"""Pure-Python helpers for the `earthlens.drought` backend.

Three small stateless utilities the backend composes:

* `snap_to_cadence` — clamp each requested date onto the source's release
  calendar (USDM Thursday-valid, EDO/GDO 10-day dekads, SPEIbase month
  start). The backend feeds the snapped list into one fetch per period
  (one FeatureCollection or one GeoTIFF each).
* `bbox_from_extent` — turn a frozen `SpatialExtent` into the
  `(west, south, east, north)` tuple every transport ultimately wants
  (the WCS `subset=Lon/Lat`, the USDM clip, the SPEIbase crop).
* Per-source attribution constants — the strings the success log line
  prints once at the end of `download` (`G6` — no `LicenseWarning`).

All helpers are module-scope (per the project's no-nested-defs rule) and
do **no** network / file I/O.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent

USDM_ATTRIBUTION: str = (
    "USDM: U.S. Drought Monitor — public-domain weekly composite produced by "
    "NDMC / UNL / USDA / NOAA. Cite the National Drought Mitigation Center."
)
"""Single-line success-log attribution for the USDM vector transport. The
backend logs this once per `download()`, not as a `LicenseWarning`
(`G6` — USDM is public domain, no warning needed)."""

EDO_ATTRIBUTION: str = (
    "EDO/GDO: Copernicus European/Global Drought Observatory (EMS) — free "
    "reuse with attribution to Copernicus EMS."
)
"""Single-line success-log attribution for the Copernicus EDO/GDO WCS
indicators. Logged once per `download()`."""

SPEIBASE_ATTRIBUTION: str = (
    "SPEIbase: CSIC Standardised Precipitation-Evapotranspiration Index "
    "database v2.10 (Vicente-Serrano et al.), CC-BY 4.0."
)
"""Single-line success-log attribution for the CSIC SPEIbase NetCDF
transport. Logged once per `download()`."""

_ATTRIBUTION_BY_TRANSPORT: dict[str, str] = {
    "usdm-geojson": USDM_ATTRIBUTION,
    "edo-wcs": EDO_ATTRIBUTION,
    "netcdf-url": SPEIBASE_ATTRIBUTION,
}


def attribution_for(transport: str) -> str:
    """Return the success-log attribution string for a catalog `transport`.

    Args:
        transport: One of `"usdm-geojson"`, `"edo-wcs"`, `"netcdf-url"`.

    Returns:
        str: The single-line attribution string to log on success.

    Raises:
        KeyError: When `transport` is not one of the three known transports.
    """
    return _ATTRIBUTION_BY_TRANSPORT[transport]


def _to_date(value: dt.date | dt.datetime | str) -> dt.date:
    """Coerce a `date` / `datetime` / `YYYY-MM-DD` string to a `date`.

    Args:
        value: Anything date-like the backend hands in.

    Returns:
        datetime.date: The calendar date.

    Raises:
        TypeError: When `value` is none of the accepted types.
        ValueError: When `value` is a string that does not parse as
            `YYYY-MM-DD`.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    raise TypeError(
        f"snap_to_cadence wants a date / datetime / 'YYYY-MM-DD' string, "
        f"got {type(value).__name__}: {value!r}"
    )


def _snap_weekly(date: dt.date) -> dt.date:
    """Snap a calendar date to the **Thursday at or before** it.

    USDM releases every Thursday UTC; the release covers the previous
    Tuesday. The dataset row stores the **release date** (Thursday) in its
    `{ymd}` URL placeholder, so the backend snaps the user's request to
    that Thursday.

    Args:
        date: A calendar date.

    Returns:
        datetime.date: The most recent Thursday at-or-before `date`.
    """
    days_back = (date.weekday() - 3) % 7
    return date - dt.timedelta(days=days_back)


def _snap_10day(date: dt.date) -> dt.date:
    """Snap a calendar date to the **start of its 10-day dekad**.

    Copernicus EDO/GDO indicators publish on the 1st, 11th, and 21st of
    each month (the WMO dekads); the dataset for a given week is the dekad
    that contains it. So the 1st–10th snap to the 1st, the 11th–20th to
    the 11th, and the 21st–end-of-month to the 21st.

    Args:
        date: A calendar date.

    Returns:
        datetime.date: The first day of the dekad that contains `date`.
    """
    if date.day <= 10:
        return date.replace(day=1)
    if date.day <= 20:
        return date.replace(day=11)
    return date.replace(day=21)


def _snap_monthly(date: dt.date) -> dt.date:
    """Snap a calendar date to the **first of its month**.

    SPEIbase publishes one observation per month, indexed at month start;
    the same is true for the EDO/GDO indicators whose cadence is `monthly`
    (e.g. `spgTS`, `twsan`).

    Args:
        date: A calendar date.

    Returns:
        datetime.date: The first day of `date`'s month.
    """
    return date.replace(day=1)


_SNAPPERS = {
    "weekly": _snap_weekly,
    "10day": _snap_10day,
    "monthly": _snap_monthly,
}


def snap_to_cadence(
    dates: list[dt.date | dt.datetime | str],
    cadence: str,
) -> list[dt.date]:
    """Clamp every requested date onto the source's release calendar.

    Returns the **distinct** snapped dates in chronological order, so an
    input range that fans into the same release / dekad / month is
    de-duplicated to a single fetch.

    Args:
        dates: One or more date-like values to snap (anything
            `_to_date` accepts).
        cadence: The source's release cadence — `"weekly"`,
            `"10day"`, or `"monthly"`. Matches the `Dataset.cadence`
            literal.

    Returns:
        list[datetime.date]: Distinct snapped dates, sorted ascending.

    Raises:
        ValueError: When `cadence` is not one of the three known values.

    Examples:
        - USDM Tuesday → snaps back to the previous Thursday release:
            ```python
            >>> import datetime as dt
            >>> from earthlens.drought._helpers import snap_to_cadence
            >>> snap_to_cadence([dt.date(2026, 6, 23)], "weekly")
            [datetime.date(2026, 6, 18)]

            ```
        - A two-week span snaps to one Thursday per week (de-duplicated):
            ```python
            >>> snap_to_cadence([dt.date(2026, 6, 23), dt.date(2026, 6, 24)], "weekly")
            [datetime.date(2026, 6, 18)]

            ```
        - A dekad date snaps to the first of its 10-day period:
            ```python
            >>> snap_to_cadence([dt.date(2026, 6, 15)], "10day")
            [datetime.date(2026, 6, 11)]

            ```
        - Month dates collapse to month-start:
            ```python
            >>> snap_to_cadence([dt.date(2026, 6, 15)], "monthly")
            [datetime.date(2026, 6, 1)]

            ```
    """
    try:
        snapper = _SNAPPERS[cadence]
    except KeyError as exc:
        raise ValueError(
            f"unknown cadence {cadence!r}; expected one of "
            f"{sorted(_SNAPPERS)}"
        ) from exc
    snapped = {snapper(_to_date(d)) for d in dates}
    return sorted(snapped)


def bbox_from_extent(
    space: SpatialExtent,
) -> tuple[float, float, float, float]:
    """Return the `(west, south, east, north)` bbox from a `SpatialExtent`.

    Every drought transport ultimately wants the same WGS-84 bbox tuple —
    the WCS `subset=Lon/Lat` axes, the USDM clip after reproject, the
    SPEIbase pyramids crop. This helper takes the frozen extent the
    `AbstractDataSource` builds in `__init__` and emits that tuple in a
    single place.

    Args:
        space: A `SpatialExtent` from the parent class.

    Returns:
        tuple[float, float, float, float]: `(west, south, east, north)` in
            EPSG:4326 degrees.
    """
    return (
        float(space.longitude_min),
        float(space.latitude_min),
        float(space.longitude_max),
        float(space.latitude_max),
    )


def days_in_month(date: dt.date) -> int:
    """Return the number of days in `date`'s month.

    Tiny wrapper kept module-scope (rather than nested in the backend) so
    the SPEIbase month-end bound is testable in isolation.

    Args:
        date: A calendar date.

    Returns:
        int: 28 / 29 / 30 / 31 depending on month and leap year.
    """
    return calendar.monthrange(date.year, date.month)[1]
