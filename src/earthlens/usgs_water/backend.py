"""Backend that fetches USGS water data from NWIS / the USGS Water Data API.

`USGSWater(AbstractDataSource)` wraps the official **`dataretrieval`**
SDK to pull time-series and discrete water observations from the U.S.
Geological Survey's National Water Information System — ~10,000 active
stream gauges and many more groundwater / water-quality sites across the
United States. A request is a bbox (or explicit `sites=`) + a time
window + a list of **NWIS parameter codes** (`["00060"]` discharge,
`["00065"]` gage height, …); the backend returns a per-site time-series
as a long-format :class:`pandas.DataFrame`, so `OUTPUT_KIND = "tabular"`
and the :class:`earthlens.earthlens.EarthLens` facade rejects an
`aggregate=` argument (use the server-side `service="statistics"`
rollup instead).

The full NWIS / Water Data service surface is selectable via a
`service=` keyword argument (default `"daily"`): `daily`,
`instantaneous`, `samples`, `statistics`, `gwlevels`,
`field-measurements`, `peaks`, `ratings`, and `sites`. The USGS is
mid-migration from the legacy `waterservices.usgs.gov` endpoint (the
`dataretrieval.nwis` module) to the modern `api.waterdata.usgs.gov`
endpoint (the `dataretrieval.waterdata` module). The `api=` keyword
selects which:

* `"auto"` (default) — try the modern endpoint, but because it
  rate-limits anonymous access aggressively (HTTP 429), transparently
  fall back to the legacy endpoint on a 429 when no token is set.
* `"waterdata"` — force the modern endpoint (a 429 surfaces as an
  error).
* `"legacy"` — force the legacy endpoint.

Authentication is an **optional** Personal Access Token (the
`API_USGS_PAT` env var or the `api_token=` argument); anonymous access
works at lower rate limits. See :class:`earthlens.usgs_water.auth`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.usgs_water.auth import UsgsWaterAuth, UsgsWaterCredentials
from earthlens.usgs_water.catalog import Catalog

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

ApiFlavour = Literal["auto", "waterdata", "legacy"]
OutputFormat = Literal["csv", "parquet"]

#: Every NWIS / Water Data service plane the backend can address. The
#: `service=` argument is validated against this tuple; the per-module
#: function names live in :data:`earthlens.usgs_water._helpers._SERVICE_FN`.
SERVICES: tuple[str, ...] = (
    "daily",
    "instantaneous",
    "samples",
    "statistics",
    "gwlevels",
    "field-measurements",
    "peaks",
    "ratings",
    "sites",
)

#: Accepted `api=` selectors (modern / legacy / auto-fallback).
API_FLAVOURS: tuple[str, ...] = ("auto", "waterdata", "legacy")

#: Accepted on-disk output formats.
OUTPUT_FORMATS: tuple[str, ...] = ("csv", "parquet")

#: Services that are keyed by site rather than parameter code, so they
#: ignore `variables` and require an explicit `sites=`.
_SITE_KEYED_SERVICES: frozenset[str] = frozenset({"peaks", "ratings"})

#: Default parameter code when `variables` is empty — discharge (cfs).
_DEFAULT_CODES: list[str] = ["00060"]


def _import_dataretrieval():
    """Import the `dataretrieval` SDK lazily with a friendly error.

    Keeps `import earthlens.usgs_water` working without the optional
    `[usgs-water]` extra: the SDK is only needed at `download()` time.

    Returns:
        The imported `dataretrieval` top-level module.

    Raises:
        ImportError: When `dataretrieval` is not installed; the message
            names the `earthlens[usgs-water]` extra to install.
    """
    try:
        import dataretrieval  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via fakes
        raise ImportError(
            "The USGS Water backend requires the 'dataretrieval' SDK. "
            "Install it with: pip install earthlens[usgs-water]"
        ) from exc
    return dataretrieval


class USGSWater(AbstractDataSource):
    """USGS NWIS / Water Data backend (long-format tabular output).

    Fetches per-site water observations for a bbox / explicit sites /
    date window / parameter-code list through the same `download()`
    shape every other earthlens backend uses, and returns a
    long-format :class:`pandas.DataFrame` (one row per observation).
    The query is a search/fetch split: :meth:`_search` enumerates the
    monitoring locations to pull, and :meth:`_fetch` pulls each
    service's observations and normalises them to one tidy long schema.

    The `service=` argument selects the NWIS / Water Data plane (see
    :data:`SERVICES`); `api=` selects the modern / legacy endpoint with
    a 429 auto-fallback (see the module docstring). Authentication is
    an optional Personal Access Token.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row site
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "daily",
        path: Path | str = "",
        fmt: str = "%Y-%m-%d",
        api_token: str | None = None,
        service: str = "daily",
        sites: list[str] | str | None = None,
        api: ApiFlavour = "auto",
        output_format: OutputFormat = "csv",
        stat_type: str = "daily",
        limit: int | None = None,
    ):
        """Initialise a USGS Water backend instance.

        Args:
            start: Inclusive start of the window, parsed with `fmt`.
            end: Inclusive end of the window.
            variables: List of NWIS parameter codes or friendly names
                (`["00060"]`, `["discharge", "gage_height"]`), resolved
                to 5-digit codes via the catalog. An empty list defaults
                to discharge (`["00060"]`). Ignored by the site-keyed
                services (`peaks`, `ratings`).
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes.
            temporal_resolution: Convenience alias mapped onto
                `service` when `service` is left at its default —
                `"daily"` keeps `daily`, a sub-daily value selects
                `instantaneous`. An explicit `service=` always wins.
            path: Output directory for the written table.
            fmt: `strptime` format for `start` / `end`.
            api_token: Optional USGS Personal Access Token; falls back
                to the `API_USGS_PAT` env var, then anonymous access.
            service: The NWIS / Water Data plane to query — one of
                :data:`SERVICES`. Defaults to `"daily"`.
            sites: Explicit USGS site number(s) to query, bypassing the
                bbox site discovery. Required for the site-keyed
                services (`peaks`, `ratings`).
            api: Endpoint selector — `"auto"` (default; modern with a
                429 fallback to legacy), `"waterdata"` (force modern),
                or `"legacy"` (force the deprecated `nwis` endpoint).
            output_format: On-disk format — `"csv"` (default) or
                `"parquet"`.
            stat_type: For `service="statistics"`, the rollup period —
                `"daily"`, `"monthly"`, or `"annual"`.
            limit: Optional cap on the rows pulled per request (passed
                through to the modern endpoint's `limit=`). `None`
                means the SDK default.

        Raises:
            ValueError: When `service`, `api`, or `output_format` is
                not a recognised value.
            TypeError: When `variables` is a mapping (this backend takes
                a flat list of parameter codes / names).
        """
        if isinstance(variables, dict):
            raise TypeError(
                "USGSWater `variables` must be a list of NWIS parameter "
                "codes or friendly names (e.g. ['00060', 'gage_height']), "
                "not a mapping. Query filters are explicit USGSWater(...) "
                "keyword arguments (service=, sites=, api=, ...)."
            )
        if service not in SERVICES:
            raise ValueError(
                f"service must be one of {list(SERVICES)}, got {service!r}."
            )
        if api not in API_FLAVOURS:
            raise ValueError(
                f"api must be one of {list(API_FLAVOURS)}, got {api!r}."
            )
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {list(OUTPUT_FORMATS)}, "
                f"got {output_format!r}."
            )

        # `service` wins; otherwise honour the temporal_resolution alias.
        if service == "daily" and temporal_resolution not in ("daily", ""):
            service = "instantaneous" if temporal_resolution != "monthly" else "daily"

        self._api_token = api_token
        self._service = service
        self._sites = [sites] if isinstance(sites, str) else sites
        self._api = api
        self._output_format: OutputFormat = output_format
        self._stat_type = stat_type
        self._limit = limit
        self._auth: UsgsWaterAuth | None = None
        self._catalog = Catalog()
        self._used_legacy_fallback = False
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_CODES),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Build :class:`UsgsWaterAuth` and resolve the optional token.

        Returns `None` (the SDK has no global client object; the token,
        when present, is exported to `API_USGS_PAT` by the auth).
        """
        self._auth = UsgsWaterAuth(UsgsWaterCredentials(api_token=self._api_token))
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        Args:
            lat_lim: `[lat_min, lat_max]` in degrees.
            lon_lim: `[lon_min, lon_max]` in degrees.

        Returns:
            SpatialExtent: Validated, frozen bbox.
        """
        return SpatialExtent.from_pairs(lat_lim=lat_lim, lon_lim=lon_lim)

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a :class:`TemporalExtent`.

        The whole window is fetched per site in one call (NWIS takes a
        start/end range), so there is no per-date loop; `dates`
        collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Recorded as the resolution label.
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`.
        """
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _resolved_codes(self) -> list[str]:
        """Resolve `variables` to 5-digit NWIS parameter codes, order-stable.

        Returns:
            list[str]: The resolved codes (de-duplicated, first-wins).

        Raises:
            ValueError: If a name is neither a known catalog entry nor a
                raw 5-digit code (with a did-you-mean hint).
        """
        codes: list[str] = []
        for name in self.vars:
            code = self._catalog.resolve(name)
            if code not in codes:
                codes.append(code)
        return codes

    def _bbox_list(self) -> list[float]:
        """Return the request bbox as modern `[west, south, east, north]`."""
        return [self.space.west, self.space.south, self.space.east, self.space.north]

    def _bbox_str(self) -> str:
        """Return the request bbox as legacy `"west,south,east,north"`."""
        return (
            f"{self.space.west},{self.space.south},"
            f"{self.space.east},{self.space.north}"
        )

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> pd.DataFrame:
        """Fetch the selected service, write the table, and return it.

        Args:
            progress_bar: Show a progress bar over the fetch.
            aggregate: Must be `None`. USGS Water output is tabular, so
                there is no gridded reduction; the facade already
                rejects a non-`None` `aggregate=` for a `tabular`
                backend, and this is the belt-and-suspenders guard for
                direct callers. Use `service="statistics"` for a
                server-side temporal rollup instead.

        Returns:
            pd.DataFrame: The long-format observation table.

        Raises:
            NotImplementedError: If `aggregate` is not `None`, or — for
                now — for services not yet wired in this scaffold (the
                values services land in C3).
        """
        if aggregate is not None:
            raise NotImplementedError(
                "USGSWater.download(aggregate=...) is not supported: USGS "
                "water observations are tabular per-site rows, not gridded "
                "rasters, so there is no meaningful gridded reduction. Use "
                "service='statistics' for a server-side temporal rollup "
                "(daily/monthly/annual) instead."
            )
        return self._api()

    def _api(self) -> pd.DataFrame:
        """Compose `_search` and `_fetch` (filled by C3 onward)."""
        raise NotImplementedError(
            "USGSWater value services are implemented in C3; the C1 scaffold "
            "only covers construction, validation, auth, and the bbox/date grid."
        )
