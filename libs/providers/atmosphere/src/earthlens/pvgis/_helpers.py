"""Pure, network-light helpers for the PVGIS backend.

Every function here is stateless and grounded in the live API facts pinned
under the A1 gate captures: the point sampler that
turns a bbox into `(lat, lon)` pairs (`point_grid`), the keyless REST URL
builder (`build_url`), the rate-limited GET with `429` backoff
(`throttled_get`), and the two JSON parsers that fold a `seriescalc` /
`tmy` response into a long-format `pandas.DataFrame` (`parse_seriescalc` /
`parse_tmy`). The parse is pure `pandas` — deliberately no array / NetCDF
layer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import pandas as pd

from earthlens.base.http import HttpClient

#: Base URL of the keyless PVGIS 5.3 non-interactive REST service.
BASE = "https://re.jrc.ec.europa.eu/api/v5_3"

#: Minimum spacing between requests to honour the 30 req/s rate limit (`G5`).
MIN_INTERVAL = 1.0 / 30.0

#: `strptime` format of the PVGIS timestamp field (`"20200101:0010"`).
TIME_FORMAT = "%Y%m%d:%H%M"


def point_grid(space: Any, spacing_deg: float) -> list[tuple[float, float]]:
    """Enumerate `(lat, lon)` sample points over a bbox at a fixed spacing.

    `SpatialExtent` exposes only `north`/`south`/`east`/`west` — there is no
    `point_grid` method on it (`G3`), so the bbox is walked here. A degenerate
    bbox (south == north and west == east, i.e. a single point) yields exactly
    one coordinate.

    Args:
        space: A `SpatialExtent` (anything with `north`/`south`/`east`/`west`
            float properties).
        spacing_deg: Grid step in degrees, applied to both axes. Must be
            positive.

    Returns:
        list[tuple[float, float]]: The `(lat, lon)` pairs, latitude-major
            (all longitudes for the south row first), each rounded to 6
            decimals.

    Raises:
        ValueError: If `spacing_deg` is not positive.

    Examples:
        - A single-point bbox yields one coordinate:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.pvgis._helpers import point_grid
            >>> pt = SpatialExtent.from_pairs(lat_lim=[45.0, 45.0], lon_lim=[8.0, 8.0])
            >>> point_grid(pt, 0.1)
            [(45.0, 8.0)]

            ```
    """
    if spacing_deg <= 0:
        raise ValueError(f"spacing_deg must be positive, got {spacing_deg!r}.")
    lats: list[float] = []
    lat = space.south
    while lat <= space.north + 1e-9:
        lats.append(round(lat, 6))
        lat += spacing_deg
    lons: list[float] = []
    lon = space.west
    while lon <= space.east + 1e-9:
        lons.append(round(lon, 6))
        lon += spacing_deg
    return [(la, lo) for la in lats for lo in lons]


def build_url(tool: str, lat: float, lon: float, params: dict[str, Any]) -> str:
    """Build a keyless PVGIS REST URL for one tool + point (`G2`).

    `lat`, `lon`, and `outputformat=json` are always present; `params`
    supplies the per-product knobs (year window, PV tilt/azimuth/peakpower,
    `raddatabase`, …) and overrides the defaults if it re-specifies them.

    Args:
        tool: The PVGIS tool / endpoint segment (`"seriescalc"`, `"tmy"`).
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        params: Extra query parameters merged after the fixed three.

    Returns:
        str: The full `https://re.jrc.ec.europa.eu/api/v5_3/<tool>?...` URL.

    Examples:
        - The fixed params lead the query string:
            ```python
            >>> from earthlens.pvgis._helpers import build_url
            >>> build_url("seriescalc", 45.0, 8.0, {"startyear": 2020})
            'https://re.jrc.ec.europa.eu/api/v5_3/seriescalc?lat=45.0&lon=8.0&outputformat=json&startyear=2020'

            ```
    """
    query = {"lat": lat, "lon": lon, "outputformat": "json", **params}
    return f"{BASE}/{tool}?{urlencode(query)}"


def throttled_get(
    session: Any,
    url: str,
    *,
    last_call: list[float],
    max_retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """GET a URL, honouring the 30 req/s limit and retrying `429` (`G5`).

    Sleeps just long enough since the previous call (tracked in the mutable
    `last_call` single-element list) to keep the per-IP request rate at or
    below the limit, then issues the GET. A non-`429` response (including a
    `400` out-of-coverage error, which the caller inspects) is returned
    immediately; a `429` is retried with exponential backoff.

    Args:
        session: A `requests.Session`-like object with a `.get(url,
            timeout=...)` method.
        url: The request URL.
        last_call: A one-element list holding the `monotonic()` timestamp of
            the previous request; updated in place after each GET so a shared
            instance throttles a whole batch.
        max_retries: How many times to retry on a `429` before raising.
        sleep: Sleep function (injected so tests run instantly).
        monotonic: Monotonic clock (injected for the same reason).

    Returns:
        The response object from the first non-`429` GET.

    Raises:
        requests.HTTPError: If every attempt returned `429` (the final
            response's `raise_for_status()` is called).
    """
    wait = MIN_INTERVAL - (monotonic() - last_call[0])
    if wait > 0:
        sleep(wait)
    # The proactive throttle above is kept here; `HttpClient` owns only the
    # 429 retry/back-off. `raise_for_status=False` lets a non-429 4xx flow back
    # to the caller (which inspects the out-of-coverage error body). The
    # `2**attempt` back-off keeps the default `max_backoff` ceiling: the old
    # loop's exponential wait never reached it, and it now bounds a server
    # `Retry-After` the old loop ignored.
    http = HttpClient(
        session=session,
        status_forcelist=(429,),
        max_retries=max_retries - 1,
        backoff_factor=1.0,
        raise_for_status=False,
        sleep=sleep,
        timeout=60,
    )
    resp = http.get(url, raise_for_status=False)
    last_call[0] = monotonic()
    if resp.status_code == 429:
        resp.raise_for_status()
    return resp


def _records_to_frame(rows: list[dict[str, Any]], time_key: str) -> pd.DataFrame:
    """Fold a list of PVGIS hourly records into a long frame (`G8`).

    The timestamp column is renamed from its API key (`"time"` for
    seriescalc, `"time(UTC)"` for TMY) to a canonical `time` and parsed to a
    **UTC-aware** `datetime64` — PVGIS timestamps are UTC (TMY labels its key
    `time(UTC)` explicitly), so the column is localised to UTC to match the
    other tabular backends rather than left naive. The value columns pass
    through unchanged.

    Args:
        rows: The `outputs.hourly` / `outputs.tmy_hourly` record list.
        time_key: The timestamp key in `rows` (`"time"` or `"time(UTC)"`).

    Returns:
        pd.DataFrame: A frame with a UTC-aware `time` (`datetime64[ns, UTC]`)
            column plus the value columns, in record order.
    """
    df = pd.DataFrame(rows)
    if time_key != "time" and time_key in df.columns:
        df = df.rename(columns={time_key: "time"})
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], format=TIME_FORMAT, utc=True)
    return df


def parse_seriescalc(payload: dict[str, Any]) -> pd.DataFrame:
    """Parse a `seriescalc` JSON response into a long DataFrame (`G8`).

    Args:
        payload: The decoded JSON (`{inputs, outputs, meta}`); the records
            live at `payload["outputs"]["hourly"]`.

    Returns:
        pd.DataFrame: One row per hourly record — a `time` `datetime64`
            column plus the radiation / PV value columns (`G(i)`, `T2m`, …,
            and `P` when `pvcalculation=1` was requested).
    """
    return _records_to_frame(payload["outputs"]["hourly"], "time")


def parse_tmy(payload: dict[str, Any]) -> pd.DataFrame:
    """Parse a `tmy` JSON response into a long DataFrame (`G8`).

    The TMY records key the timestamp as `"time(UTC)"` (not `"time"`); it is
    normalised to a `time` column here.

    Args:
        payload: The decoded JSON; the records live at
            `payload["outputs"]["tmy_hourly"]`.

    Returns:
        pd.DataFrame: One row per TMY hour — a `time` `datetime64` column
            plus the meteorological value columns (`T2m`, `RH`, `G(h)`, …).
    """
    return _records_to_frame(payload["outputs"]["tmy_hourly"], "time(UTC)")


def error_message(payload: dict[str, Any]) -> str:
    """Pull the human-readable `message` from a PVGIS error body (`G6`).

    PVGIS error responses are `{"message": <str>, "status": <int>}`.

    Args:
        payload: The decoded JSON error body (or any mapping).

    Returns:
        str: The `message` field, or `""` when absent.
    """
    return str(payload.get("message", "")) if isinstance(payload, dict) else ""


def empty_canonical(columns: list[str]) -> pd.DataFrame:
    """Return a zero-row frame with the given columns (the no-data fallback).

    Args:
        columns: The canonical column names for the product.

    Returns:
        pd.DataFrame: An empty frame carrying exactly `columns`.
    """
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
