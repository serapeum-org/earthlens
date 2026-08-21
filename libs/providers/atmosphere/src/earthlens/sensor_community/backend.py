"""Backend that fetches crowdsourced air-quality data from Sensor.Community.

`SensorCommunity(AbstractDataSource)` returns readings from the
Sensor.Community low-cost-sensor network as a long-format
`pandas.DataFrame` (one row per measurement), the same `tabular` shape as
`earthlens.openaq`.

This is a `tabular` backend: the result is per-row station observations,
not a gridded array, so `OUTPUT_KIND = "tabular"` and the
`earthlens.earthlens.EarthLens` facade rejects an `aggregate=` argument.

Transport (a search/fetch split, like OpenAQ). The archive has one CSV
per (sensor, day) but no bbox index, so `_search` first hits the **live
JSON API** (`data.sensor.community`) to discover which sensors are active
in the request bbox; `_fetch` then pulls each discovered sensor's
per-day **archive** CSV (`archive.sensor.community`) over the date range,
`;`-parses it, and extracts the requested pollutant columns. A missing
daily file is logged and skipped (never a silent gap). Because discovery
uses the live snapshot, historical coverage is limited to sensors
*currently* reporting in the bbox.

Data quality: readings are crowdsourced from low-cost sensors and
licensed under the ODbL; every `download()` emits a `LicenseWarning`.

Pollutant selection: `variables` is a `list[str]` of pollutant names
(`["pm25"]`, `["pm25", "pm10"]`, `["temperature", "humidity"]`), mapped
to CSV columns + serving sensor types via the bundled catalog.
"""

from __future__ import annotations

import datetime as dt
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd
from loguru import logger

from earthlens.base import (
    AbstractDataSource,
    OutputKind,
    RemoteProduct,
    TemporalExtent,
    to_datetime,
)
from earthlens.sensor_community._helpers import (
    LicenseWarning,
    SensorCommunityClient,
    empty_frame,
    frame_from_csv,
    sensors_in_bbox,
)
from earthlens.sensor_community.catalog import Catalog

if TYPE_CHECKING:
    import requests


FileFormat = Literal["csv", "parquet"]

#: Default pollutant when `variables` is empty.
_DEFAULT_PARAMETERS = ["pm25"]

#: `temporal_resolution` labels accepted by this backend. Sensor.Community
#: archives raw sub-minute readings with no server-side rollup, so the
#: label is recorded for provenance only. `"daily"` is accepted (the
#: facade default).
_ACCEPTED_RESOLUTIONS = frozenset({"raw", "hourly", "daily"})

#: The ODbL / crowdsourced-quality caveat text.
_LICENSE_TEXT = (
    "Sensor.Community data is crowdsourced from low-cost sensors and licensed "
    "under the ODbL: redistribution must preserve attribution + share-alike, "
    "and readings are not reference-grade. Treat values accordingly."
)


class SensorCommunity(AbstractDataSource):
    """Sensor.Community air-quality backend (long-format tabular output).

    Discovers active sensors in the request bbox via the live JSON API,
    then fetches each sensor's per-day archive CSV over the date window,
    returning a long-format `pandas.DataFrame` (one row per measurement).
    There is no authentication — both hosts are public.

    Attributes:
        OUTPUT_KIND: `"tabular"` — the result is per-row station
            observations, so the facade rejects `aggregate=` with
            `NotImplementedError`.
    """

    OUTPUT_KIND: OutputKind = "tabular"

    AGGREGATE_REFUSAL_REASON = "readings are tabular per-row station data, not gridded rasters, so there is no meaningful gridded reduction"

    def __init__(
        self,
        start: str,
        end: str,
        variables: list[str],
        lat_lim: list[float],
        lon_lim: list[float],
        temporal_resolution: str = "raw",
        path: Path | str | None = None,
        fmt: str = "%Y-%m-%d",
        session: requests.Session | None = None,
        client: SensorCommunityClient | None = None,
        file_format: FileFormat = "csv",
    ):
        """Initialise a Sensor.Community backend instance.

        Args:
            start: Inclusive start of the observation window, as a string
                parsed with `fmt`.
            end: Inclusive end of the observation window.
            variables: List of pollutant names to fetch (`["pm25"]`,
                `["temperature", "humidity"]`). Mapped to CSV columns +
                serving sensor types via the catalog. An empty list
                defaults to `["pm25"]`.
            lat_lim: `[lat_min, lat_max]` bounding-box latitudes in
                degrees.
            lon_lim: `[lon_min, lon_max]` bounding-box longitudes in
                degrees.
            temporal_resolution: Recorded for provenance; Sensor.Community
                has no server-side rollup. Accepts `"raw"` (default),
                `"hourly"`, or `"daily"` (the facade default).
            path: Output directory for the written CSV / Parquet. Created
                by the parent class if absent.
            fmt: `strptime` format for `start` / `end`.
            session: An existing `requests.Session` to reuse (used to
                build the default client). Injectable for tests.
            client: A `SensorCommunityClient` to reuse. Injectable so
                tests supply a fake transport; when `None` (default) one
                is built lazily from `session`.
            file_format: Output format — `"csv"` (default) or `"parquet"`.
        """
        if isinstance(variables, dict):
            raise TypeError(
                "SensorCommunity `variables` must be a list of pollutant names "
                "(e.g. ['pm25', 'pm10']), not a mapping."
            )
        self._session = session
        self._client_obj = client
        self._file_format: FileFormat = file_format
        self._catalog = Catalog()
        self._columns: dict[str, str] | None = None
        self._units: dict[str, str] | None = None
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

    def _check_input_dates(
        self,
        start: str,
        end: str,
        temporal_resolution: str,
        fmt: str,
    ) -> TemporalExtent:
        """Parse the `[start, end]` window into a `TemporalExtent`.

        Args:
            start: Inclusive start date string.
            end: Inclusive end date string.
            temporal_resolution: Provenance label (`"raw"`, `"hourly"`, or
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

    def _client(self) -> SensorCommunityClient:
        """Build (once) and return the injectable HTTP client.

        Returns:
            SensorCommunityClient: The cached client (injected, or built
                from `session`).
        """
        if self._client_obj is None:
            self._client_obj = SensorCommunityClient(session=self._session)
        return self._client_obj

    def _days(self) -> list[str]:
        """Return the `YYYY-MM-DD` archive days spanning the request window.

        Returns:
            list[str]: One date string per day from `start_date` to
                `end_date`, inclusive.
        """
        start = self.time.start_date.date()
        end = self.time.end_date.date()
        span = (end - start).days
        return [
            (start + dt.timedelta(days=offset)).isoformat()
            for offset in range(span + 1)
        ]

    def _column_map(self) -> dict[str, str]:
        """CSV column -> pollutant name for the requested pollutants (cached)."""
        if self._columns is None:
            self._columns = self._catalog.columns_for(cast("list[str]", self.vars))
        return self._columns

    def _unit_map(self) -> dict[str, str]:
        """Pollutant name -> reporting unit for the requested pollutants (cached)."""
        if self._units is None:
            self._units = {
                name: self._catalog.get_pollutant(name).units for name in self.vars
            }
        return self._units

    def _search(self) -> list[RemoteProduct]:
        """Discover active sensors in the bbox via the live JSON API.

        Returns:
            list[RemoteProduct]: One product per unique `(sensor_id,
                sensor_type)` active in the bbox whose type serves a
                requested pollutant; `id` is the sensor id and `metadata`
                carries `sensor_type` / `lat` / `lon`.
        """
        wanted = self._catalog.sensor_types_for(cast("list[str]", self.vars))
        snapshot = self._client().live_snapshot()
        sensors = sensors_in_bbox(
            snapshot,
            (self.space.south, self.space.north),
            (self.space.west, self.space.east),
            wanted,
        )
        if not sensors:
            logger.warning(
                "Sensor.Community search: no live sensor of the requested "
                "type(s) is currently reporting in the bbox; historical "
                "coverage is limited to sensors active now."
            )
        return [
            RemoteProduct(id=sensor["sensor_id"], metadata=sensor) for sensor in sensors
        ]

    def _fetch_one(self, product: RemoteProduct) -> pd.DataFrame:
        """Fetch one sensor's per-day archive CSVs over the date window.

        Args:
            product: One `RemoteProduct` from `_search`.

        Returns:
            pd.DataFrame: The sensor's readings in the long schema (empty
                when it reported nothing across the window).
        """
        columns = self._column_map()
        units = self._unit_map()
        sensor_type = product.metadata["sensor_type"]
        frames: list[pd.DataFrame] = []
        for day in self._days():
            text = self._client().archive_csv(day, sensor_type, product.id)
            if text is None:
                logger.info(
                    f"Sensor.Community: no archive file for sensor "
                    f"{product.id} ({sensor_type}) on {day}; skipped."
                )
                continue
            frames.append(
                frame_from_csv(text, columns, units, default_sensor_type=sensor_type)
            )
        non_empty = [frame for frame in frames if not frame.empty]
        if not non_empty:
            return empty_frame()
        combined = pd.concat(non_empty, ignore_index=True)
        # `concat` copied every frame, so the per-sensor sources are dead
        # weight from here. Both lists have to be cleared — either alone frees
        # nothing, since each holds the same frames. This does not lower the
        # peak (reached inside the concat) but stops them being carried through
        # the window filter and the return.
        frames.clear()
        non_empty.clear()
        return combined

    def _api(self) -> list[pd.DataFrame]:
        """Compose `_search` and `_fetch_one` into the canonical C3 shape.

        Contract-only: `download` overrides the write path and calls
        `_search_fetch_each` directly (to concat + window itself), mirroring
        the `earthlens.openaq` sibling, so this is not on the live path.
        """
        return self._search_fetch_each(desc="Sensor.Community sensors", unit="sensor")

    def _window(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Return the `[lower, upper)` UTC filter bounds for the request.

        A date-granular end (midnight) is extended by one day so the whole
        end day is inclusive — the common path, and identical to AirNow /
        EEA. A non-midnight `end` (only reachable via an hour-aware `fmt`)
        yields a **half-open** `[start, end)` window here; AirNow instead
        treats its end hour as inclusive (its API takes an hourly range), so
        hour-granular callers should account for that one-endpoint
        difference.

        Returns:
            tuple[pd.Timestamp, pd.Timestamp]: `(lower, upper)`, tz-aware
                UTC; `upper` is exclusive.
        """
        lower = pd.Timestamp(self.time.start_date, tz="UTC")
        end = self.time.end_date
        if end.hour == 0 and end.minute == 0:
            upper = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
        else:
            upper = pd.Timestamp(end, tz="UTC")
        return lower, upper

    def download(
        self,
        progress_bar: bool = True,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Discover + fetch readings, write them to `path`, return the frame.

        Emits a `LicenseWarning` (ODbL), runs the live-API discovery then
        the per-sensor archive fetch under a `tqdm` bar, concatenates and
        windows the readings to the exact date range, writes the
        long-format result to `path` as CSV (or Parquet), and returns it.
        An empty result returns — and writes — a schema-only DataFrame.

        Args:
            progress_bar: Show the per-sensor `tqdm` bar. Defaults to
                `True`.
            limit: Cap on the total readings fetched, across every discovered
                sensor. Applied as each sensor's frame arrives, so a sensor
                past the cap never has its daily archive files downloaded.
                `None` (the default) fetches everything. The cap is on rows
                *fetched*, before the window filter, so the returned frame can
                be shorter than the cap.

        Returns:
            pd.DataFrame: The long-format readings (schema columns,
                `datetime_utc` tz-aware UTC). Empty (schema-only) when
                nothing matched.
        """
        self._limit = self.check_limit(limit)
        warnings.warn(_LICENSE_TEXT, LicenseWarning, stacklevel=2)

        frames = self._search_fetch_each(
            progress_bar=progress_bar, desc="Sensor.Community sensors", unit="sensor"
        )
        non_empty = [frame for frame in frames if not frame.empty]
        # Release the per-sensor frames as we go: `concat` copies, so holding
        # the sources alongside the combined frame — and then alongside the
        # windowed copy — keeps up to three full copies of the request in RAM.
        frames.clear()
        if non_empty:
            combined = pd.concat(non_empty, ignore_index=True)
            non_empty.clear()
            lower, upper = self._window()
            mask = (combined["datetime_utc"] >= lower) & (
                combined["datetime_utc"] < upper
            )
            df = combined[mask].reset_index(drop=True)
            del combined
        else:
            df = empty_frame()

        out_path = self._output_path()
        if self._file_format == "parquet":
            df.to_parquet(out_path, index=False)
        else:
            df.to_csv(out_path, index=False)

        if len(df):
            logger.info(
                f"Sensor.Community download summary: {len(df)} reading(s) "
                f"across {df['station_id'].nunique()} sensor(s) written to "
                f"{out_path}"
            )
        else:
            logger.warning(
                "Sensor.Community download summary: no readings matched the "
                f"request; wrote an empty (schema-only) frame to {out_path}"
            )
        return df

    def _output_path(self) -> Path:
        """Compose the per-request output file path under `root_dir`."""
        ext = "parquet" if self._file_format == "parquet" else "csv"
        params = "-".join(self.vars)
        start = self.time.start_date.strftime("%Y%m%d")
        end = self.time.end_date.strftime("%Y%m%d")
        return self.root_dir / f"sensor_community_{params}_{start}_{end}.{ext}"
