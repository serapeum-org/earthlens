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
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from loguru import logger
from tqdm import tqdm

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    SpatialExtent,
    TemporalExtent,
)
from earthlens.openaq.auth import OpenaqAuth, OpenaqCredentials
from earthlens.openaq.catalog import Catalog
from earthlens.openaq.client import OpenaqClient

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
        self._client_obj: OpenaqClient | None = None
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

    def _client(self) -> OpenaqClient:
        """Build (once) and return the rate-limit-aware HTTP client.

        The client is created lazily — after :meth:`_initialize` has
        resolved the API key — and cached on the instance, so a
        multi-sensor `_fetch` reuses one `requests.Session`.

        Returns:
            OpenaqClient: The cached client bound to the resolved key.

        Raises:
            AuthenticationError: When the auth was never configured
                (reading :attr:`OpenaqAuth.api_key` before
                `configure()`).
        """
        if self._client_obj is None:
            assert self._auth is not None  # set in _initialize, before download
            self._client_obj = OpenaqClient(
                self._auth.api_key, max_retries=5, backoff_factor=1.0
            )
        return self._client_obj

    def _bbox(self) -> str:
        """Return the request bbox as OpenAQ's `"west,south,east,north"`."""
        return (
            f"{self.space.west},{self.space.south},"
            f"{self.space.east},{self.space.north}"
        )

    def _search(self) -> list[RemoteProduct]:
        """Enumerate one :class:`RemoteProduct` per matching sensor.

        Lists monitoring locations in the bbox filtered by the
        requested parameter ids (cheap — locations + sensor
        enumeration, no measurement bytes), then emits one product per
        sensor whose parameter was requested, honouring
        `max_locations` and `max_sensors_per_location`. A truncating
        cap logs a loud warning so the caller knows the frame is
        partial.

        Returns:
            list[RemoteProduct]: One product per sensor; `id` is the
                sensor id and `metadata` carries `station_id` /
                `parameter` / `units` / `lat` / `lon` / `provider`.
        """
        wanted_ids = set(self._catalog.ids_for(self.vars))
        locations = self._client().list_locations(
            bbox=self._bbox(),
            parameters_id=sorted(wanted_ids),
            limit=self._page_limit,
            max_locations=self._max_locations,
        )
        if self._max_locations is not None and len(locations) >= self._max_locations:
            logger.warning(
                f"OpenAQ search hit the max_locations={self._max_locations} cap; "
                "the result may be partial. Raise max_locations= or shrink the "
                "bbox / date window to capture every station."
            )

        products: list[RemoteProduct] = []
        for location in locations:
            coords = location.get("coordinates") or {}
            provider = (location.get("provider") or {}).get("name") or "openaq"
            sensors = location.get("sensors") or []
            if self._max_sensors_per_location is not None:
                sensors = sensors[: self._max_sensors_per_location]
            for sensor in sensors:
                parameter = sensor.get("parameter") or {}
                if parameter.get("id") not in wanted_ids:
                    continue
                products.append(
                    RemoteProduct(
                        id=str(sensor.get("id")),
                        metadata={
                            "station_id": location.get("id"),
                            "parameter": parameter.get("name"),
                            "units": parameter.get("units"),
                            "lat": coords.get("latitude"),
                            "lon": coords.get("longitude"),
                            "provider": provider,
                        },
                    )
                )
        return products

    def _fetch(self, products: list[RemoteProduct]) -> list[pd.DataFrame]:
        """Pull each sensor's measurements into a per-product DataFrame.

        Widens the inherited `-> list[Path]` contract: a tabular
        backend returns in-memory long-format
        :class:`pandas.DataFrame`s, not file paths (the `R4` finding —
        the composition is sound because `_api_via_search_fetch` only
        short-circuits on the empty product list and otherwise returns
        this list verbatim). Each sensor's measurements are fetched
        with `429`/`Retry-After` back-off via :class:`OpenaqClient`.

        Args:
            products: The list returned by :meth:`_search`.

        Returns:
            list[pd.DataFrame]: One schema-shaped frame per product, in
                the same order; an empty (schema-only) frame for a
                sensor with no measurements in the window.
        """
        return [self._fetch_one(product) for product in products]

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch one sensor's measurements and shape them to the schema.

        Args:
            product: One :class:`RemoteProduct` from :meth:`_search`.

        Returns:
            pd.DataFrame: The sensor's rows in the long-format schema
                (empty, schema-only, when the sensor returned nothing).
        """
        measurements = self._client().list_measurements(
            sensor_id=product.id,
            datetime_from=self.time.start_date.isoformat(),
            datetime_to=self.time.end_date.isoformat(),
            rollup=self._rollup,
            limit=self._page_limit,
        )
        rows = [_measurement_row(product, m) for m in measurements]
        if not rows:
            return _empty_frame()
        return pd.DataFrame(rows).astype(_SCHEMA)

    def download(
        self,
        progress_bar: bool = True,
        aggregate: AggregationConfig | None = None,
    ) -> pd.DataFrame:
        """Fetch measurements, write them to `path`, and return the frame.

        Runs the cheap :meth:`_search` (locations + sensors) then the
        rate-limited :meth:`_fetch` (per-sensor measurements), wrapping
        the per-sensor loop in a `tqdm` progress bar. The per-product
        frames are concatenated into one long-format DataFrame, written
        to `path` as CSV (or Parquet), and returned. An empty result
        returns — and writes — a schema-only DataFrame so callers
        always get the same shape.

        Args:
            progress_bar: Show a per-sensor progress bar. Defaults to
                `True`.
            aggregate: Must be `None`. OpenAQ output is tabular, so
                there is no meaningful gridded reduction; the facade
                already rejects a non-`None` `aggregate=` for a
                `tabular` backend, and this is the belt-and-suspenders
                guard for direct backend callers. Use the server-side
                `temporal_resolution` rollup instead.

        Returns:
            pd.DataFrame: The long-format union of every sensor's
                measurements (schema columns, `datetime_utc` tz-aware
                UTC). Empty (schema-only) when nothing matched.

        Raises:
            NotImplementedError: If `aggregate` is not `None`.
        """
        if aggregate is not None:
            raise NotImplementedError(
                "OpenAQ.download(aggregate=...) is not supported: pollutant "
                "measurements are tabular per-row station observations, not "
                "gridded rasters, so there is no meaningful gridded "
                "reduction. Use the server-side temporal_resolution rollup "
                "(hourly/daily/monthly/yearly) instead."
            )

        products = self._search()
        iterator = tqdm(
            products,
            disable=not progress_bar,
            desc="OpenAQ sensors",
            unit="sensor",
        )
        frames = [self._fetch_one(product) for product in iterator]
        non_empty = [frame for frame in frames if not frame.empty]
        df = pd.concat(non_empty, ignore_index=True) if non_empty else _empty_frame()

        out_path = self._output_path()
        if self._file_format == "parquet":
            df.to_parquet(out_path, index=False)
        else:
            df.to_csv(out_path, index=False)

        if len(df):
            logger.info(
                f"OpenAQ download summary: {len(df)} measurement(s) across "
                f"{len(non_empty)} sensor(s) written to {out_path}"
            )
        else:
            logger.warning(
                "OpenAQ download summary: no measurements matched the request; "
                f"wrote an empty (schema-only) frame to {out_path}"
            )
        return df

    def _output_path(self) -> Path:
        """Compose the per-request output file path under `root_dir`."""
        ext = "parquet" if self._file_format == "parquet" else "csv"
        params = "-".join(self.vars)
        start = self.time.start_date.strftime("%Y%m%d")
        end = self.time.end_date.strftime("%Y%m%d")
        return self.root_dir / f"openaq_{params}_{start}_{end}.{ext}"

    def _api(self):
        """Compose `_search` and `_fetch` into the canonical C3 shape."""
        return self._api_via_search_fetch()


def _empty_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the exact long-format schema.

    Used for a no-data sensor and for a download that matched nothing,
    so every caller sees the same columns and dtypes regardless of
    whether any measurements came back.

    Returns:
        pd.DataFrame: Zero rows, :data:`_SCHEMA` columns and dtypes.
    """
    return pd.DataFrame({column: [] for column in _SCHEMA}).astype(_SCHEMA)


def _measurement_datetime(measurement: dict[str, Any]) -> Any:
    """Extract the UTC timestamp from one v3 measurement object.

    The v3 shape nests the timestamp under `period.datetimeFrom.utc`;
    older / rollup shapes use a flat `datetime.utc` or `date.utc`. This
    tries each in turn so the backend tolerates the pre-v1 surface
    drift the plan flags.

    Args:
        measurement: One measurement result object.

    Returns:
        The UTC timestamp string, or `None` if no known field is
            present.
    """
    period = measurement.get("period") or {}
    datetime_from = period.get("datetimeFrom") or {}
    if isinstance(datetime_from, dict) and datetime_from.get("utc"):
        return datetime_from["utc"]
    for key in ("datetime", "date"):
        value = measurement.get(key) or {}
        if isinstance(value, dict) and value.get("utc"):
            return value["utc"]
    return None


def _measurement_row(
    product: RemoteProduct, measurement: dict[str, Any]
) -> dict[str, Any]:
    """Build one schema row from a product's metadata + a measurement.

    The station-level fields (`station_id` / `parameter` / `units` /
    `lat` / `lon` / `provider`) come from the product metadata captured
    in :meth:`OpenAQ._search`; only `value` and `datetime_utc` come
    from the measurement object.

    Args:
        product: The sensor's :class:`RemoteProduct`.
        measurement: One measurement result object for that sensor.

    Returns:
        dict[str, Any]: A row keyed by the :data:`_SCHEMA` columns.
    """
    meta = product.metadata
    return {
        "station_id": meta.get("station_id"),
        "parameter": meta.get("parameter"),
        "datetime_utc": pd.to_datetime(_measurement_datetime(measurement), utc=True),
        "value": measurement.get("value"),
        "units": meta.get("units"),
        "lat": meta.get("lat"),
        "lon": meta.get("lon"),
        "provider": meta.get("provider"),
    }
