"""Pure, network-light helpers for the NREL backend.

Every function here is stateless and grounded in the live API facts pinned
under the A1 gate captures: the point sampler that turns
a bbox into `(lat, lon)` pairs (`point_grid`), the keyed REST CSV URL builder
(`build_url`), the rate-limited GET honouring the 1 req/s limit
(`throttled_get`), and the CSV parser that folds an NSRDB PSM v4 / TMY / WIND
Toolkit response into a long-format `pandas.DataFrame` (`parse_psm3_csv`). The
parse is pure `pandas` — deliberately no array / NetCDF / HSDS layer and no
heavy gridded-archive SDK.

`NLR_HOST` is the single source of truth for the host: `developer.nrel.gov` was
retired 2026-05-29 (NREL renamed → "National Laboratory of the Rockies / NLR"),
so the API now lives at `https://developer.nlr.gov`.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests

from earthlens.base.http import HttpClient

#: Base host of the keyed NREL/NLR Developer Network REST service. The legacy
#: `developer.nrel.gov` host was retired 2026-05-29; keep the host here so a
#: future change is one edit.
NLR_HOST = "https://developer.nlr.gov"

#: Minimum spacing between CSV requests to honour the 1 req/s rate limit. The
#: CSV format is capped at 5000 req/day and at most 1 request per second.
MIN_INTERVAL = 1.0

#: The data-table header common to every NSRDB / WTK CSV: the first line that
#: begins with this marks the boundary between the metadata header rows and the
#: data table (NSRDB has 2 metadata rows, WTK has 1, so the offset is detected
#: rather than hard-coded).
_DATA_HEADER_PREFIX = "Year,Month,Day,Hour,Minute"

#: The five integer date-part columns the timestamp is assembled from.
_TIME_PARTS = ["Year", "Month", "Day", "Hour", "Minute"]


def point_grid(space: Any, spacing_deg: float) -> list[tuple[float, float]]:
    """Enumerate `(lat, lon)` sample points over a bbox at a fixed spacing.

    `SpatialExtent` exposes only `north`/`south`/`east`/`west` — there is no
    `point_grid` method on it, so the bbox is walked here. A degenerate bbox
    (south == north and west == east, i.e. a single point) yields exactly one
    coordinate.

    Args:
        space: A `SpatialExtent` (anything with `north`/`south`/`east`/`west`
            float properties).
        spacing_deg: Grid step in degrees, applied to both axes. Must be
            positive.

    Returns:
        list[tuple[float, float]]: The `(lat, lon)` pairs, latitude-major (all
            longitudes for the south row first), each rounded to 6 decimals.

    Raises:
        ValueError: If `spacing_deg` is not positive.

    Examples:
        - A single-point bbox yields one coordinate:
            ```python
            >>> from earthlens.base import SpatialExtent
            >>> from earthlens.nrel._helpers import point_grid
            >>> pt = SpatialExtent.from_pairs(lat_lim=[39.7, 39.7], lon_lim=[-105.2, -105.2])
            >>> point_grid(pt, 0.05)
            [(39.7, -105.2)]

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


def build_url(
    endpoint: str,
    lat: float,
    lon: float,
    names: Any,
    attributes: list[str],
    *,
    api_key: str,
    email: str,
    interval: int = 60,
    utc: str = "false",
) -> str:
    """Build a keyed NREL CSV download URL for one point + one year.

    The CSV download endpoints serve a single `POINT(lon lat)` for a single
    `names=` (a year, or `tmy`) per call. `api_key` and `email` are passed as
    plain strings — the caller resolves the `SecretStr` with
    `.get_secret_value()` only here, so the secret never lives in a long-lived
    object.

    Args:
        endpoint: The CSV endpoint path (e.g.
            `/api/nsrdb/v2/solar/nsrdb-GOES-aggregated-v4-0-0-download.csv`).
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        names: The dataset year (e.g. `2020`) or `"tmy"`.
        attributes: The comma-joined variable list (e.g. `["ghi", "dni"]`).
        api_key: The resolved NREL API key (plain string).
        email: The registered contact email.
        interval: Data resolution in minutes (`30` or `60`).
        utc: `"true"` for UTC timestamps, `"false"` for local time.

    Returns:
        str: The full `https://developer.nlr.gov<endpoint>?...` URL.

    Examples:
        - The point is encoded as WKT and the key/email ride along:
            ```python
            >>> from earthlens.nrel._helpers import build_url
            >>> url = build_url(
            ...     "/api/nsrdb/v2/solar/nsrdb-GOES-aggregated-v4-0-0-download.csv",
            ...     39.74, -105.18, 2020, ["ghi", "dni"],
            ...     api_key="KEY", email="me@example.com",
            ... )
            >>> url.startswith("https://developer.nlr.gov/api/nsrdb/v2/solar/")
            True
            >>> "wkt=POINT%28-105.18+39.74%29" in url
            True
            >>> "names=2020" in url and "attributes=ghi%2Cdni" in url
            True

            ```
    """
    query = {
        "api_key": api_key,
        "email": email,
        "wkt": f"POINT({lon} {lat})",
        "names": names,
        "attributes": ",".join(attributes),
        "interval": interval,
        "utc": utc,
    }
    return f"{NLR_HOST}{endpoint}?{urlencode(query)}"


def throttled_get(
    session: Any,
    url: str,
    *,
    last_call: list[float],
    max_retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """GET a URL, honouring the 1 req/s limit and retrying `429`.

    Sleeps just long enough since the previous call (tracked in the mutable
    `last_call` single-element list) to keep the per-key request rate at or
    below 1 req/s, then issues the GET. A non-`429` response (including a `4xx`
    out-of-coverage / bad-request error, which the caller inspects) is returned
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
    # Proactive 1 req/s throttle: kept here (not delegated to HttpClient's
    # `min_interval`) because it draws on the shared `last_call` list so a whole
    # batch of points throttles against one timestamp, not per-client.
    wait = MIN_INTERVAL - (monotonic() - last_call[0])
    if wait > 0:
        sleep(wait)
    # HttpClient owns only the 429 retry/back-off here: `max_retries - 1`
    # additional retries reproduce the old loop's `max_retries` total GETs with
    # `backoff_factor * 2**attempt` (= `2**attempt`) waits — one fewer than the
    # old loop only on the all-429 exhaustion path, where it dropped a wasted
    # trailing sleep before raising. `raise_for_status` is off so a non-429 4xx
    # flows back to the caller unraised for inspection.
    http = HttpClient(
        session=session,
        status_forcelist=(429,),
        max_retries=max_retries - 1,
        backoff_factor=1.0,
        raise_for_status=False,
        sleep=sleep,
        timeout=120,
    )
    resp = http.get(url, raise_for_status=False)
    last_call[0] = monotonic()
    if resp.status_code == 429:
        # Retries exhausted on a persistent 429. Raise an HTTPError (as the old
        # loop's raise_for_status() did) but WITHOUT the URL — it carries the
        # `api_key=` query param, and requests.HTTPError echoes `resp.url`.
        raise requests.HTTPError(
            f"NREL returned HTTP 429 after {max_retries} attempts "
            "(the api_key has been redacted from this error)."
        )
    return resp


def _data_header_offset(text: str) -> int:
    """Find the number of metadata rows before the data table.

    Scans for the first line beginning with `Year,Month,Day,Hour,Minute` — the
    NSRDB CSV opens with 2 metadata rows and the WTK CSV with 1, so the offset
    is detected rather than assumed.

    Args:
        text: The raw CSV body.

    Returns:
        int: The count of leading rows to skip (the index of the data header).

    Raises:
        ValueError: If no data-table header is found in the body.
    """
    for index, line in enumerate(text.splitlines()):
        if line.startswith(_DATA_HEADER_PREFIX):
            return index
    raise ValueError(
        "NREL CSV has no 'Year,Month,Day,Hour,Minute' data-table header; the "
        "response is not a recognised PSM3 / WTK CSV (it may be an error body)."
    )


def parse_psm3_csv(text: str, *, meta_rows: int | None = None) -> pd.DataFrame:
    """Parse an NSRDB PSM v4 / TMY / WTK CSV into a long DataFrame.

    The CSV opens with metadata rows (Source / Location / Lat / Lon / Elevation
    for NSRDB, a single `SiteID,…` row for WTK) then the data table
    (`Year,Month,Day,Hour,Minute,GHI,DNI,…`). The metadata header is skipped
    and a canonical `time` column is assembled from the five date-part columns.
    The `time` column is **timezone-naive wall-clock**: it carries whatever the
    request's `utc=` flag asked for (local site time for `utc="false"`, the NREL
    default; UTC for `utc="true"`) without attaching a tzinfo, matching the
    naive timestamps the CSV ships. Pure `pandas` (`read_csv` / `io.StringIO`);
    no array-library detour.

    Args:
        text: The raw CSV body.
        meta_rows: Explicit count of leading metadata rows to skip. When `None`
            (the default), the data-table header is auto-detected so the same
            call handles NSRDB (2 rows) and WTK (1 row).

    Returns:
        pd.DataFrame: One row per record — a leading `time` `datetime64` column
            (assembled from `Year/Month/Day/Hour/Minute`, which are then dropped
            as redundant) followed by the data value columns.

    Raises:
        ValueError: If `meta_rows` is `None` and no data-table header is found.

    Examples:
        - A tiny two-metadata-row NSRDB-shaped CSV parses to one row, `time`
          first and the raw date parts dropped:
            ```python
            >>> from earthlens.nrel._helpers import parse_psm3_csv
            >>> csv = (
            ...     "Source,Location ID,Latitude,Longitude\\n"
            ...     "NSRDB,1,39.73,-105.18\\n"
            ...     "Year,Month,Day,Hour,Minute,GHI,DNI\\n"
            ...     "2020,1,1,0,30,0,0\\n"
            ... )
            >>> df = parse_psm3_csv(csv)
            >>> len(df)
            1
            >>> df["time"].dtype.kind
            'M'
            >>> list(df.columns)
            ['time', 'GHI', 'DNI']

            ```
    """
    skip = meta_rows if meta_rows is not None else _data_header_offset(text)
    data = pd.read_csv(io.StringIO(text), skiprows=skip)
    time = pd.to_datetime(data[_TIME_PARTS])
    # The five raw date-part columns are now redundant with `time`; drop them
    # and lead with `time` so the frame is `[time, <value columns>]` — matching
    # the catalog's documented `columns` and the empty_canonical fallback schema.
    data = data.drop(columns=_TIME_PARTS)
    data.insert(0, "time", time)
    return data


def empty_canonical(columns: list[str]) -> pd.DataFrame:
    """Return a zero-row frame with the given columns (the no-data fallback).

    Args:
        columns: The canonical column names for the product.

    Returns:
        pd.DataFrame: An empty frame carrying exactly `columns`.
    """
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})
