"""Backend that fetches ground-station air-quality data from OpenAQ v3.

`OpenAQ(AbstractDataSource)` queries the OpenAQ v3 web service — the
aggregator of >180 air-quality monitoring networks worldwide (US
AirNow, EEA, UK AURN, Sensor.Community, and many national networks) —
and returns ground-station pollutant measurements as a long-format
:class:`pandas.DataFrame` (one row per measurement).

This is the package's first `tabular` backend: the result is a table
of per-row station observations, not a gridded array, so
`OUTPUT_KIND = "tabular"` and the :class:`earthlens.earthlens.EarthLens`
facade rejects an `aggregate=` argument (there is no meaningful
gridded reduction of a pollutant timeseries; use the server-side
`temporal_resolution` rollup instead).

HTTP path: a thin `requests`-based client
(:class:`earthlens.openaq.client.OpenaqClient`) owns pagination and
the `429`/`Retry-After` rate-limit back-off, rather than the official
(pre-v1, unstable) `openaq` SDK — `requests` is already a core
earthlens dependency, so this adds none, and the rate-limit handling
(the backend's main risk on a large bbox) stays under our control.

Parameter selection follows the OpenAQ-specific reading of
`variables` (see the package docstring): `variables` is a `list[str]`
of pollutant parameter names (`["pm25"]`, `["pm25", "no2"]`), resolved
to OpenAQ numeric `parameters_id` via the bundled catalog; query
filters (`max_locations`, `temporal_resolution` rollup, the date
window) arrive as explicit constructor keyword arguments.
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
from earthlens.openaq.auth import OpenaqAuth, OpenaqCredentials
from earthlens.openaq.catalog import Catalog

if TYPE_CHECKING:
    from earthlens.aggregate import AggregationConfig

FileFormat = Literal["csv", "parquet"]

#: Default pollutant when `variables` is empty.
_DEFAULT_PARAMETERS = ["pm25"]

#: Map a `temporal_resolution` label to the OpenAQ v3 server-side rollup
#: endpoint segment. The sentinel ``"all"`` / ``"raw"`` selects raw
#: measurements (no rollup). Mapping cuts request volume sharply for
#: long windows — a year of daily rollups is ~365 rows per sensor, not
#: tens of thousands of raw readings.
_ROLLUP_BY_RESOLUTION: dict[str, str | None] = {
    "hourly": "hours",
    "daily": "days",
    "monthly": "months",
    "yearly": "years",
    "all": None,
    "raw": None,
}

#: Long-format schema (column -> dtype) every `download()` returns, even
#: for an empty result, so callers always get the same shape.
_SCHEMA: dict[str, str] = {
    "station_id": "object",
    "parameter": "object",
    "datetime_utc": "datetime64[ns, UTC]",
    "value": "float64",
    "units": "object",
    "lat": "float64",
    "lon": "float64",
    "provider": "object",
}


class OpenAQ(AbstractDataSource):
    """OpenAQ v3 air-quality backend (long-format tabular output).

    Fetches ground-station pollutant measurements for a bbox / date
    window / parameter list through the same `download()` shape every
    other earthlens backend uses, and returns a long-format
    :class:`pandas.DataFrame` (one row per measurement). The query is
    a search/fetch split: :meth:`_search` enumerates one product per
    sensor (cheap — locations + sensor enumeration, no measurement
    bytes), and :meth:`_fetch` pulls the measurements per sensor with
    rate-limit back-off.

    Authentication is a single free `X-API-Key`, resolved once by
    :meth:`_initialize` via :class:`OpenaqAuth` (explicit `api_key=`
    then the `OPENAQ_API_KEY` env var).

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row station
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`. This backend is the package's first
            `tabular` exercise of that facade guard.
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
        api_key: str | None = None,
        max_locations: int | None = 500,
        max_sensors_per_location: int | None = None,
        limit: int = 1000,
        file_format: FileFormat = "csv",
    ):
        """Initialise an OpenAQ backend instance.

        Args:
            start: Inclusive start of the measurement window, as a
                string parsed with `fmt`.
            end: Inclusive end of the measurement window.
            variables: List of pollutant parameter names to fetch
                (`["pm25"]`, `["pm25", "no2"]`). For this backend
                `variables` names the *pollutant parameters*, not data
                variables (see the package docstring); they are
                resolved to OpenAQ numeric `parameters_id` via the
                catalog. An empty list defaults to `["pm25"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: Selects the OpenAQ server-side rollup
                endpoint — `"hourly"` -> `/hours`, `"daily"` ->
                `/days` (the default, matching the facade default),
                `"monthly"` -> `/months`, `"yearly"` -> `/years`. The
                sentinel `"all"` (or `"raw"`) fetches raw measurements
                with no rollup. Note: a facade user who omits this gets
                `"daily"` rollup, not raw rows.
            path: Output directory for the written CSV / Parquet.
                Created by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            api_key: OpenAQ `X-API-Key`. Falls back to the
                `OPENAQ_API_KEY` environment variable.
            max_locations: Cap on the number of monitoring locations
                enumerated by `_search`, to bound the rate-limited
                fan-out. `None` means no cap. Defaults to `500`.
            max_sensors_per_location: Optional cap on sensors taken
                from each location. `None` (default) means no cap.
            limit: Page size for the paginated OpenAQ list endpoints.
                Defaults to `1000`.
            file_format: Output format — `"csv"` (default) or
                `"parquet"`.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "OpenAQ `variables` must be a list of pollutant names (e.g. "
                "['pm25', 'no2']), not a mapping. For this backend "
                "`variables` selects pollutant parameters, not data "
                "variables; query filters are explicit OpenAQ(...) keyword "
                "arguments."
            )
        self._api_key = api_key
        self._max_locations = max_locations
        self._max_sensors_per_location = max_sensors_per_location
        self._page_limit = limit
        self._file_format: FileFormat = file_format
        self._auth: OpenaqAuth | None = None
        self._catalog = Catalog()
        super().__init__(
            start=start,
            end=end,
            variables=list(variables) or list(_DEFAULT_PARAMETERS),
            temporal_resolution=temporal_resolution,
            lat_lim=lat_lim,
            lon_lim=lon_lim,
            fmt=fmt,
            path=path,
        )

    def _initialize(self):
        """Build :class:`OpenaqAuth` and resolve the API key.

        Returns `None` so the parent binds no opaque client object —
        OpenAQ has no global SDK client; the resolved key lives on
        `self._auth` and is attached per-request by the HTTP client.

        Raises:
            AuthenticationError: When :meth:`OpenaqAuth.configure`
                finds no key (no `api_key=` and no `OPENAQ_API_KEY`).
        """
        self._auth = OpenaqAuth(OpenaqCredentials(api_key=self._api_key))
        self._auth.configure()
        return None

    def _create_grid(self, lat_lim: list, lon_lim: list) -> SpatialExtent:
        """Wrap the WGS84 bbox into a :class:`SpatialExtent` (no snapping).

        OpenAQ takes a bbox of min/max lat/lon directly, so the box
        passes through unchanged.

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

        OpenAQ fetches the whole window per sensor in one paginated
        call (optionally rolled up server-side), so there is no
        per-date loop; `dates` collapses to the two endpoints. The
        `temporal_resolution` label is recorded as the resolution and
        also drives the rollup-endpoint choice in `_fetch`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: The rollup label (`"hourly"`,
                `"daily"`, `"monthly"`, `"yearly"`, or `"all"` / `"raw"`
                for no rollup).
            fmt: `strptime` format applied to `start` and `end`.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `start` parses to a date later than `end`,
                or `temporal_resolution` is not a recognised label.
        """
        if temporal_resolution not in _ROLLUP_BY_RESOLUTION:
            raise ValueError(
                f"temporal_resolution must be one of "
                f"{sorted(_ROLLUP_BY_RESOLUTION)}, got {temporal_resolution!r}."
            )
        start_dt = dt.datetime.strptime(start, fmt)
        end_dt = dt.datetime.strptime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    @property
    def _rollup(self) -> str | None:
        """The OpenAQ rollup-endpoint segment for this request, or `None`.

        `None` means "raw measurements" (the `"all"` / `"raw"`
        sentinel); otherwise one of `"hours"` / `"days"` / `"months"`
        / `"years"`.
        """
        return _ROLLUP_BY_RESOLUTION[self.time.resolution]

    def _api(self):
        """Compose `_search` and `_fetch` into the canonical C3 shape.

        The search/fetch bodies land in C2; this wires the standard
        composition so the abstract contract is satisfied from C1.
        """
        return self._api_via_search_fetch()

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> pd.DataFrame:
        """Fetch measurements and return the long-format DataFrame.

        Implemented in C2. The C1 scaffold only wires authentication
        and the request extents.

        Args:
            progress_bar: Whether to show a per-sensor progress bar.
            aggregate: Must be `None` — OpenAQ output is tabular, so
                the facade rejects a non-`None` `aggregate=`.

        Raises:
            NotImplementedError: Always, until C2 lands the
                search/fetch implementation.
        """
        raise NotImplementedError(
            "OpenAQ.download is implemented in task C2 (search/fetch + "
            "rate-limit back-off); the C1 scaffold only wires auth and "
            "the request extents."
        )
