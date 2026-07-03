"""Client, parsing, and licence helpers for the Sensor.Community backend.

Sensor.Community exposes two hosts the backend needs:

* the **live JSON API** (`data.sensor.community`) — the last ~5 minutes of
  every sensor globally, bbox-filterable, used to *discover* which sensors
  are active in the request bbox (the archive has no bbox index);
* the **archive** (`archive.sensor.community`) — one CSV per (sensor, day),
  `<date>/<date>_<sensor_type>_sensor_<id>.csv`, `;`-separated, used to
  *fetch* each discovered sensor's history over the date range.

`SensorCommunityClient` wraps an injectable `requests.Session` over both
hosts with `429`/`Retry-After` back-off; a missing archive file (`404`)
returns `None` so the backend can log-and-skip without failing the whole
request. `LicenseWarning` flags the ODbL / crowdsourced-quality caveat.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
import requests
from loguru import logger

#: Live JSON API: the last ~5 minutes of every sensor, globally.
LIVE_URL = "https://data.sensor.community/static/v2/data.json"

#: Archive host; a per-sensor daily CSV lives at
#: `{ARCHIVE_URL}/{date}/{date}_{sensor_type}_sensor_{id}.csv`.
ARCHIVE_URL = "https://archive.sensor.community"

#: Provider label stamped on every returned row.
PROVIDER = "sensor.community"

#: Long-format schema (column -> dtype) the backend returns, even for an
#: empty result, so callers always get the same shape.
SCHEMA: dict[str, str] = {
    "station_id": "object",
    "sensor_type": "object",
    "parameter": "object",
    "datetime_utc": "datetime64[ns, UTC]",
    "value": "float64",
    "units": "object",
    "lat": "float64",
    "lon": "float64",
    "provider": "object",
}


class LicenseWarning(UserWarning):
    """Warns that Sensor.Community data carries ODbL / quality obligations.

    Sensor.Community measurements are crowdsourced from low-cost sensors
    and licensed under the Open Database License (ODbL): redistribution
    must keep the attribution and share-alike terms, and the readings are
    not reference-grade. The backend emits this once per `download()` so a
    downstream user is told rather than discovering it silently.
    """


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds.

    Args:
        value: The raw `Retry-After` header value, or `None`.

    Returns:
        The delay in seconds, or `None` when absent / unparseable.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class SensorCommunityClient:
    """Injectable client over the Sensor.Community live + archive hosts.

    Attributes:
        max_retries: Maximum number of `429` retries before raising.
        backoff_factor: Base seconds for exponential back-off when no
            `Retry-After` header is present (wait = factor * 2**attempt).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a client over both Sensor.Community hosts.

        Args:
            session: An existing `requests.Session` to reuse. Defaults to a
                fresh session. Injectable so tests supply a fake transport.
            max_retries: Maximum `429` retries before raising.
            backoff_factor: Base seconds for exponential back-off.
            timeout: Per-request timeout in seconds.
            sleep: The sleep function used between retries. Defaults to
                `time.sleep`; injectable so tests run without real delays.
        """
        self._session = session if session is not None else requests.Session()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self._sleep = sleep

    def _get(self, url: str) -> requests.Response:
        """GET `url`, retrying on `429` with `Retry-After`-aware back-off.

        Args:
            url: The absolute URL to fetch.

        Returns:
            requests.Response: The final response (caller inspects the
                status).

        Raises:
            requests.HTTPError: Only via the caller's `raise_for_status`;
                this method does not raise on status by itself.
        """
        attempt = 0
        while True:
            response = self._session.get(url, timeout=self.timeout)
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after
                    if retry_after is not None
                    else self.backoff_factor * (2**attempt)
                )
                logger.warning(
                    "Sensor.Community rate-limited (429); retry "
                    f"{attempt + 1}/{self.max_retries} after {wait:.1f}s"
                )
                self._sleep(wait)
                attempt += 1
                continue
            return response

    def live_snapshot(self) -> list[dict[str, Any]]:
        """Fetch the live JSON API's last-~5-minute global sensor snapshot.

        Returns:
            list[dict[str, Any]]: The array of live measurement records
                (each with `location` and `sensor` sub-objects).

        Raises:
            requests.HTTPError: On a non-`429` error status.
        """
        response = self._get(LIVE_URL)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def archive_csv(
        self, date: str, sensor_type: str, sensor_id: str
    ) -> str | None:
        """Fetch one per-sensor daily archive CSV, or `None` when absent.

        Args:
            date: The archive day as `YYYY-MM-DD`.
            sensor_type: The archive sensor-type slug (`"sds011"`).
            sensor_id: The sensor's numeric id (as a string).

        Returns:
            str | None: The CSV text, or `None` when the file does not
                exist (`404`) — the sensor did not report that day.

        Raises:
            requests.HTTPError: On a non-`404`, non-`429` error status.
        """
        url = f"{ARCHIVE_URL}/{date}/{date}_{sensor_type}_sensor_{sensor_id}.csv"
        response = self._get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text


def sensors_in_bbox(
    snapshot: list[dict[str, Any]],
    lat_lim: tuple[float, float],
    lon_lim: tuple[float, float],
    wanted_types: set[str],
) -> list[dict[str, Any]]:
    """Filter a live snapshot to unique sensors in the bbox of wanted types.

    Args:
        snapshot: The live JSON API records from `live_snapshot`.
        lat_lim: `(lat_min, lat_max)` of the request bbox.
        lon_lim: `(lon_min, lon_max)` of the request bbox.
        wanted_types: Archive sensor-type slugs (lower-case) to keep.

    Returns:
        list[dict[str, Any]]: One entry per unique `(sensor_id,
            sensor_type)` — `{"sensor_id", "sensor_type", "lat", "lon"}` —
            sorted by sensor id for determinism.
    """
    lat_min, lat_max = sorted(lat_lim)
    lon_min, lon_max = sorted(lon_lim)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for record in snapshot:
        location = record.get("location") or {}
        sensor = record.get("sensor") or {}
        sensor_type = ((sensor.get("sensor_type") or {}).get("name") or "").lower()
        if sensor_type not in wanted_types:
            continue
        try:
            lat = float(location.get("latitude"))
            lon = float(location.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        sensor_id = str(sensor.get("id"))
        key = (sensor_id, sensor_type)
        if key not in seen:
            seen[key] = {
                "sensor_id": sensor_id,
                "sensor_type": sensor_type,
                "lat": lat,
                "lon": lon,
            }
    return [seen[key] for key in sorted(seen)]


def frame_from_csv(
    text: str,
    columns: dict[str, str],
    units: dict[str, str],
) -> pd.DataFrame:
    """Reshape one per-sensor archive CSV into the backend's long schema.

    For each requested pollutant whose CSV `column` is present, emits one
    row per reading (`station_id` / `sensor_type` / `lat` / `lon` /
    `timestamp` come from the CSV). Rows with a non-numeric value or
    unparseable timestamp are dropped.

    Args:
        text: The `;`-separated CSV text of one (sensor, day) file.
        columns: CSV column -> pollutant name for the requested pollutants
            (`{"P2": "pm25", "P1": "pm10"}`).
        units: Pollutant name -> reporting unit string.

    Returns:
        pd.DataFrame: The readings in the `SCHEMA` columns / dtypes; empty
            when the CSV has none of the requested columns.
    """
    raw = pd.read_csv(io.StringIO(text), sep=";")
    present = {col: name for col, name in columns.items() if col in raw.columns}
    if raw.empty or not present:
        return empty_frame()
    frames: list[pd.DataFrame] = []
    # Normalise the CSV's upper-case sensor type (`SDS011`) to the lower-case
    # archive slug (`sds011`) so the output column matches the discovery
    # metadata and the archive URL.
    sensor_type = raw["sensor_type"].astype(str).str.lower() if "sensor_type" in raw else None
    for col, name in present.items():
        sub = pd.DataFrame(index=raw.index)
        sub["station_id"] = raw["sensor_id"].astype(str)
        sub["sensor_type"] = sensor_type
        sub["parameter"] = name
        sub["datetime_utc"] = pd.to_datetime(
            raw["timestamp"], utc=True, errors="coerce"
        )
        sub["value"] = pd.to_numeric(raw[col], errors="coerce")
        sub["units"] = units.get(name, "")
        sub["lat"] = pd.to_numeric(raw["lat"], errors="coerce")
        sub["lon"] = pd.to_numeric(raw["lon"], errors="coerce")
        sub["provider"] = PROVIDER
        frames.append(sub)
    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["value", "datetime_utc"]).reset_index(drop=True)
    if out.empty:
        return empty_frame()
    return out.astype(SCHEMA)


def empty_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the exact long-format schema.

    Returns:
        pd.DataFrame: Zero rows, `SCHEMA` columns and dtypes.
    """
    return pd.DataFrame({column: [] for column in SCHEMA}).astype(SCHEMA)
