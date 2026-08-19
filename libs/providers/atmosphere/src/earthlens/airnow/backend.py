"""Backend that fetches ground-station air-quality data from AirNow.

`AirNow(AbstractDataSource)` queries the US EPA / Environment Canada
AirNow `/aq/data/` bounding-box service — reference-grade hourly monitor
observations for North America — and returns them as a long-format
`pandas.DataFrame` (one row per measurement), the same `tabular` shape
as `earthlens.openaq.OpenAQ`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `OUTPUT_KIND = "tabular"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument
(there is no meaningful gridded reduction of a pollutant timeseries).

HTTP path: a thin `requests`-based client
(`earthlens.airnow.client.AirnowClient`) owns the `API_KEY` argument and
the `429`/`Retry-After` back-off. Unlike OpenAQ the endpoint returns
every matching observation in one JSON array, so there is no pagination
and no per-sensor fan-out — one authenticated GET per `download()`.

Parameter selection follows the same reading of `variables` as the
sibling AQ backends: `variables` is a `list[str]` of pollutant names
(`["pm25"]`, `["pm25", "o3"]`), resolved to AirNow `parameters` codes
via the bundled catalog. The bbox comes from `lat_lim` / `lon_lim`; the
date window from `start` / `end`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pandas as pd
from loguru import logger
from pydantic import SecretStr

from earthlens.airnow.auth import AirnowAuth, AirnowCredentials, AuthenticationError
from earthlens.airnow.catalog import Catalog
from earthlens.airnow.client import AirnowClient
from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    TemporalExtent,
    to_datetime,
)

if TYPE_CHECKING:
    import requests


FileFormat = Literal["csv", "parquet"]

#: AirNow `dataType`: `A`=AQI only, `C`=concentration only, `B`=both.
DataType = Literal["A", "C", "B"]

#: AirNow `monitorType` label -> query code (0=permanent, 1=mobile, 2=both).
_MONITOR_TYPE_CODE: dict[str, int] = {"permanent": 0, "mobile": 1, "both": 2}

#: Default pollutant when `variables` is empty.
_DEFAULT_PARAMETERS = ["pm25"]

#: `temporal_resolution` labels accepted by this backend. The `/aq/data/`
#: endpoint has no server-side rollup argument — it returns hourly monitor
#: observations — so the label is recorded for provenance but does not
#: change the request. `"daily"` is accepted because it is the facade
#: default.
_ACCEPTED_RESOLUTIONS = frozenset({"hourly", "daily"})

#: Long-format schema (column -> dtype) every `download()` returns, even
#: for an empty result, so callers always get the same shape.
_SCHEMA: dict[str, str] = {
    "station_id": "object",
    "parameter": "object",
    "datetime_utc": "datetime64[ns, UTC]",
    "value": "float64",
    "raw_value": "float64",
    "units": "object",
    "aqi": "float64",
    "category": "float64",
    "lat": "float64",
    "lon": "float64",
    "site_name": "object",
    "provider": "object",
}

#: AirNow encodes "no AQI reported" as the sentinel -999; scrub it to NaN.
_MISSING_SENTINEL = -999


class AirNow(AbstractDataSource):
    """AirNow `/aq/data/` air-quality backend (long-format tabular output).

    Fetches reference-grade hourly monitor observations for a bbox / date
    window / pollutant list through the same `download()` shape every
    other earthlens backend uses, and returns a long-format
    `pandas.DataFrame` (one row per measurement). Unlike OpenAQ there is a
    single request: the `/aq/data/` endpoint returns every matching
    observation in one JSON array.

    Authentication is a single free `API_KEY`, resolved once by
    `_initialize` via `AirnowAuth` (explicit `api_key=` then the
    `AIRNOW_API_KEY` env var).

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row station
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "monitor observations are tabular per-row station data, not gridded rasters, so there is no meaningful gridded reduction"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "hourly",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        api_key: str | None = None,
        data_type: DataType = "B",
        monitor_type: str = "both",
        include_raw_concentrations: bool = False,
        session: requests.Session | None = None,
        file_format: FileFormat = "csv",
    ):
        """Initialise an AirNow backend instance.

        Args:
            start: Inclusive start of the observation window, as a string
                parsed with `fmt`.
            end: Inclusive end of the observation window.
            variables: List of pollutant names to fetch (`["pm25"]`,
                `["pm25", "o3"]`). For this backend `variables` names the
                *pollutants*, not data variables; they are resolved to
                AirNow `parameters` codes via the catalog. An empty list
                defaults to `["pm25"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees, both in `[-90, 90]`.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees, both in `[-180, 180]`.
            temporal_resolution: Recorded for provenance. AirNow's
                `/aq/data/` endpoint returns hourly monitor observations
                and has no server-side rollup, so this label does not
                change the request. Accepts `"hourly"` (default) or
                `"daily"` (the facade default).
            path: Output directory for the written CSV / Parquet. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            api_key: AirNow `API_KEY`. Falls back to the `AIRNOW_API_KEY`
                environment variable.
            data_type: AirNow `dataType` — `"A"` (AQI only), `"C"`
                (concentration only), or `"B"` (both, the default). The
                `value` column mirrors AirNow's `Value` field verbatim, so
                its meaning follows `data_type`: for `"C"` / `"B"` it is the
                reported concentration (with the AQI in `aqi`); for `"A"`
                (AQI-only) AirNow's `Value` holds the AQI, so prefer `"B"`
                unless you specifically want AQI in `value`.
            monitor_type: Which monitors to include — `"permanent"`,
                `"mobile"`, or `"both"` (default).
            include_raw_concentrations: When `True`, ask AirNow to include
                the raw (unadjusted) concentration, which populates the
                `raw_value` column of the returned frame. Defaults to
                `False`, leaving `raw_value` as `NaN`.
            session: An existing `requests.Session` to reuse. Injectable
                so tests can supply a fake transport.
            file_format: Output format — `"csv"` (default) or `"parquet"`.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "AirNow `variables` must be a list of pollutant names (e.g. "
                "['pm25', 'o3']), not a mapping. For this backend `variables` "
                "selects pollutants; query filters are explicit AirNow(...) "
                "keyword arguments."
            )
        if monitor_type not in _MONITOR_TYPE_CODE:
            raise ValueError(
                f"monitor_type must be one of {sorted(_MONITOR_TYPE_CODE)}, "
                f"got {monitor_type!r}."
            )
        self._api_key = api_key
        self._data_type: DataType = data_type
        self._monitor_type = monitor_type
        self._include_raw = include_raw_concentrations
        self._session = session
        self._file_format: FileFormat = file_format
        self._auth: AirnowAuth | None = None
        self._client_obj: AirnowClient | None = None
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
        """Build `AirnowAuth` and resolve the API key.

        Returns `None` so the parent binds no opaque client object —
        AirNow has no global SDK client; the resolved key lives on
        `self._auth` and is attached per-request by the HTTP client.

        Raises:
            AuthenticationError: When `AirnowAuth.configure` finds no key
                (no `api_key=` and no `AIRNOW_API_KEY`).
        """
        self._auth = AirnowAuth(
            AirnowCredentials(
                api_key=None if self._api_key is None else SecretStr(self._api_key)
            )
        )
        self._auth.configure()
        return None

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a `TemporalExtent`.

        AirNow fetches the whole window in one request, so there is no
        per-date loop; `dates` collapses to the two endpoints.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Provenance label (`"hourly"` or
                `"daily"`); does not change the request.
            fmt: `strptime` format tried first for a string `start` /
                `end`; a non-matching string falls back to an ISO-8601
                parse, and a `datetime` / `date` ignores it.

        Returns:
            TemporalExtent: Frozen model with the parsed endpoints.

        Raises:
            ValueError: If `temporal_resolution` is not accepted, or
                `start` parses to a date later than `end`.
        """
        if temporal_resolution not in _ACCEPTED_RESOLUTIONS:
            raise ValueError(
                f"temporal_resolution must be one of "
                f"{sorted(_ACCEPTED_RESOLUTIONS)}, got {temporal_resolution!r}."
            )
        start_dt = to_datetime(start, fmt)
        end_dt = to_datetime(end, fmt)
        return TemporalExtent(
            start_date=start_dt,
            end_date=end_dt,
            resolution=temporal_resolution,
            dates=pd.DatetimeIndex([start_dt, end_dt]),
        )

    def _client(self) -> AirnowClient:
        """Build (once) and return the rate-limit-aware HTTP client.

        The client is created lazily — after `_initialize` has resolved
        the API key — and cached on the instance.

        Returns:
            AirnowClient: The cached client bound to the resolved key.

        Raises:
            AuthenticationError: When the auth was never configured.
        """
        if self._client_obj is None:
            if self._auth is None:  # pragma: no cover - _initialize always runs first
                raise AuthenticationError(
                    "AirNow auth was not initialised; construct the backend "
                    "through its normal __init__ before calling download()."
                )
            self._client_obj = AirnowClient(
                self._auth.api_key,
                session=self._session,
                max_retries=5,
                backoff_factor=1.0,
            )
        return self._client_obj

    def _bbox(self) -> str:
        """Return the request bbox as AirNow's `"minLon,minLat,maxLon,maxLat"`."""
        return (
            f"{self.space.west},{self.space.south},{self.space.east},{self.space.north}"
        )

    def _date_bounds(self) -> tuple[str, str]:
        """Return the `startDate` / `endDate` strings in AirNow's `YYYY-MM-DDTHH`.

        AirNow observations are hourly, so the window is expressed as an
        inclusive hour range. For the common date-granular request (both
        endpoints parse to midnight) the end is extended to the end day's
        final hour (`T23`) so a single-day `[d, d]` window returns all 24
        hours rather than only hour 0 — identical to the EEA / Sensor.Community
        full-end-day inclusion. An explicit non-midnight hour (from an
        hour-aware `fmt`) is passed through as an **inclusive** end hour here;
        the EEA / Sensor.Community client-side filters instead treat a
        non-midnight `end` as a half-open `[start, end)` bound, so
        hour-granular callers should account for that one-endpoint difference.

        Returns:
            tuple[str, str]: `(start_date, end_date)` for the query.
        """
        start = self.time.start_date
        end = self.time.end_date
        start_str = start.strftime("%Y-%m-%dT%H")
        if end.hour == 0 and end.minute == 0:
            end_str = end.strftime("%Y-%m-%dT23")
        else:
            end_str = end.strftime("%Y-%m-%dT%H")
        return start_str, end_str

    def _request_params(self) -> dict[str, Any]:
        """Assemble the `/aq/data/` query arguments for this request.

        Returns:
            dict[str, Any]: `BBOX`, `parameters`, `startDate`, `endDate`,
                `dataType`, `monitorType`, `verbose`, and
                `includerawconcentrations`. `API_KEY` / `format` are added
                by the client.
        """
        start_date, end_date = self._date_bounds()
        return {
            "BBOX": self._bbox(),
            "parameters": ",".join(
                self._catalog.codes_for(cast("list[str]", self.vars))
            ),
            "startDate": start_date,
            "endDate": end_date,
            "dataType": self._data_type,
            "monitorType": _MONITOR_TYPE_CODE[self._monitor_type],
            "verbose": 1,
            "includerawconcentrations": int(self._include_raw),
        }

    def _api(self) -> pd.DataFrame:
        """Fetch the observations for this request and shape them.

        Returns:
            pd.DataFrame: The long-format frame (empty, schema-only, when
                nothing matched).
        """
        rows = self._client().get_data(self._request_params())
        shaped = [_observation_row(row) for row in rows]
        if not shaped:
            return _empty_frame()
        return pd.DataFrame(shaped).astype(_SCHEMA)

    def download(
        self,
        progress_bar: bool = True,
    ) -> pd.DataFrame:
        """Fetch observations, write them to `path`, and return the frame.

        Runs the single `/aq/data/` request, writes the long-format
        result to `path` as CSV (or Parquet), and returns it. An empty
        result returns — and writes — a schema-only DataFrame so callers
        always get the same shape.

        Args:
            progress_bar: Accepted for API parity with the other backends;
                AirNow is a single request, so there is no per-item bar.

        Returns:
            pd.DataFrame: The long-format observations (schema columns,
                `datetime_utc` tz-aware UTC). Empty (schema-only) when
                nothing matched.
        """
        df = self._api()

        out_path = self._output_path()
        if self._file_format == "parquet":
            df.to_parquet(out_path, index=False)
        else:
            df.to_csv(out_path, index=False)

        if len(df):
            logger.info(
                f"AirNow download summary: {len(df)} observation(s) written "
                f"to {out_path}"
            )
        else:
            logger.warning(
                "AirNow download summary: no observations matched the request; "
                f"wrote an empty (schema-only) frame to {out_path}"
            )
        return df

    def _output_path(self) -> Path:
        """Compose the per-request output file path under `root_dir`."""
        ext = "parquet" if self._file_format == "parquet" else "csv"
        params = "-".join(self.vars)
        start = self.time.start_date.strftime("%Y%m%d")
        end = self.time.end_date.strftime("%Y%m%d")
        return self.root_dir / f"airnow_{params}_{start}_{end}.{ext}"


def _empty_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the exact long-format schema.

    Used for a download that matched nothing, so every caller sees the
    same columns and dtypes regardless of whether any observations came
    back.

    Returns:
        pd.DataFrame: Zero rows, `_SCHEMA` columns and dtypes.
    """
    return pd.DataFrame({column: [] for column in _SCHEMA}).astype(_SCHEMA)


def _scrub_sentinel(value: Any) -> Any:
    """Map AirNow's -999 "not reported" sentinel to `None`.

    Args:
        value: A numeric field from an observation row.

    Returns:
        `None` when `value` is the -999 sentinel, otherwise `value`.
    """
    return None if value == _MISSING_SENTINEL else value


def _observation_row(observation: dict[str, Any]) -> dict[str, Any]:
    """Build one schema row from an AirNow `/aq/data/` observation object.

    Args:
        observation: One monitor observation object from the JSON array.

    Returns:
        dict[str, Any]: A row keyed by the `_SCHEMA` columns.
    """
    # The `/aq/data/` endpoint returns the concentration under `Value`
    # (with `RawConcentration` for the unadjusted value); older/other
    # AirNow surfaces spell it `Concentration`, so fall back to that.
    concentration = observation.get("Value")
    if concentration is None:
        concentration = observation.get("Concentration")
    return {
        "station_id": observation.get("FullAQSCode"),
        "parameter": observation.get("Parameter"),
        "datetime_utc": pd.to_datetime(observation.get("UTC"), utc=True),
        "value": _scrub_sentinel(concentration),
        # `RawConcentration` (unadjusted) is present only when the request set
        # `include_raw_concentrations`; NaN otherwise.
        "raw_value": _scrub_sentinel(observation.get("RawConcentration")),
        "units": observation.get("Unit"),
        "aqi": _scrub_sentinel(observation.get("AQI")),
        "category": _scrub_sentinel(observation.get("Category")),
        "lat": observation.get("Latitude"),
        "lon": observation.get("Longitude"),
        "site_name": observation.get("SiteName"),
        "provider": observation.get("AgencyName"),
    }
