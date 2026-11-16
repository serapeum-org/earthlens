"""Fetch NHC operational best-track (ATCF `btk`) files for current-season storms.

The reanalysis sources the Tropycal backend reads for `product="besttrack"`
(HURDAT2 / IBTrACS) only publish a season months after it ends, so a storm
that has already dissipated is in neither the reanalysis nor the
`product="realtime"` active list. This module fills that gap: it reads the
National Hurricane Center's **operational** best-track files directly,

```
https://ftp.nhc.noaa.gov/atcf/btk/b{basin}{nn}{year}.dat   e.g. bep122026.dat
```

where `basin` is the ATCF prefix (`al` North Atlantic, `ep` East Pacific,
`cp` Central Pacific), `nn` the cyclone number, and `year` the season. The
files are public, need no credentials, and are updated as each storm is
analysed. The operational best track is **preliminary** until NHC finishes
its post-season reanalysis, so rows fetched here are stamped `source="nhc"`
by the backend to mark them operational.

Each file is comma-delimited ATCF text, one line per `(fix, wind-radius
threshold)`. The 34 / 50 / 64-kt wind radii live on **extra rows for the
same timestamp**, so `parse_btk_file` folds every row of a fix into one
record, reading the radii (nautical miles, NE / SE / SW / NW per threshold)
from the field positions:

| ATCF field (0-based) | Content |
|---|---|
| 2 | time, `YYYYMMDDHH` UTC |
| 4 | track type (`BEST` rows only are kept) |
| 6, 7 | lat, lon in tenths of a degree with an N/S/E/W suffix |
| 8 | max sustained wind, kt |
| 9 | min sea-level pressure, hPa |
| 10 | storm type (`DB`, `TD`, `TS`, `HU`, `LO`, …) |
| 11 | wind threshold for this row: 0, 34, 50 or 64 |
| 13-16 | radii for that threshold, nm, in NE / SE / SW / NW order |
| 27 | storm name |

This module imports only `requests` (a core dependency) and the stdlib, so
it needs none of the optional `[tropycal]` SDK: the operational best track
is plain text the backend parses itself. The resulting per-storm DataFrames
carry the same `id` / `name` / `time` / `lat` / `lon` / `vmax` / `mslp` /
`type` / `wmo_basin` columns a tropycal `Storm.to_dataframe()` frame does
(so `earthlens.tropycal.events` maps them unchanged) plus the twelve
`RADII_COLUMNS`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Protocol

import pandas as pd
import requests
from loguru import logger

from earthlens.tropycal.events import RADII_COLUMNS, RADII_QUADRANTS, RADII_THRESHOLDS


class _HttpResponse(Protocol):
    """The slice of a `requests` response this module uses.

    A structural type so the injectable `get` can be `requests.get` in
    production or a lightweight test double, as long as it yields something
    with a `text` body and a `raise_for_status` method. `text` is a
    read-only property so `requests.Response` (whose `text` is a property)
    structurally matches.
    """

    @property
    def text(self) -> str:
        """The response body as text."""
        ...

    def raise_for_status(self) -> None:
        """Raise for an error status (return value unused)."""
        ...


#: Base URL of NHC's ATCF best-track directory (trailing slash required so a
#: storm file name appends cleanly).
BTK_BASE_URL = "https://ftp.nhc.noaa.gov/atcf/btk/"

#: HTTP timeout (seconds) for the directory listing and each storm file.
HTTP_TIMEOUT = 60

#: earthlens basin code -> ATCF basin prefix, for the three basins NHC
#: serves operational best tracks for. These are the only basins
#: `product="nhc_btk"` accepts (Central Pacific has no reanalysis-basin
#: equivalent in the tropycal catalog, so it is NHC-operational-only).
NHC_BTK_BASINS: dict[str, str] = {
    "north_atlantic": "al",
    "east_pacific": "ep",
    "central_pacific": "cp",
}

#: Thresholds that carry radii (as a set, for the parse-time membership test).
_RADII_THRESHOLD_SET = frozenset(RADII_THRESHOLDS)

#: Field-27 placeholders that are not a real storm name.
_NAME_PLACEHOLDERS = frozenset({"INVEST", "UNNAMED", "NONAME", "NAMELESS"})

#: Minimum ATCF field count for a usable row (through the radii quadrants).
_MIN_FIELDS = 17

#: ATCF cyclone numbers at or above this are INVEST / genesis areas, not storms.
_INVEST_NUMBER = 90

#: A storm-file name: `b` + basin prefix + 2-digit cyclone number + 4-digit
#: year + `.dat` (the capture groups give the number and year).
_FILE_RE_TEMPLATE = r"b{prefix}(\d{{2}})(\d{{4}})\.dat"


def fetch_btk_listing(
    *,
    base_url: str = BTK_BASE_URL,
    get: Callable[..., _HttpResponse] = requests.get,
    timeout: float = HTTP_TIMEOUT,
) -> str | None:
    """Fetch the ATCF `btk` directory index, or `None` (logged) on failure.

    The `btk/` directory is shared across all basins, so a multi-basin
    request fetches this index once and passes it to each `fetch_btk_frames`
    call via `listing=`, rather than re-listing the directory per basin.

    Args:
        base_url: The ATCF best-track directory URL (overridable in tests).
        get: The HTTP getter, `requests.get` by default; injected in tests.
        timeout: Per-request timeout in seconds.

    Returns:
        The directory index HTML, or `None` when it could not be read.
    """
    return _get_text(f"{base_url}", get=get, timeout=timeout, what="btk listing")


def fetch_btk_frames(
    basin: str,
    years: Iterable[int],
    *,
    base_url: str = BTK_BASE_URL,
    get: Callable[..., _HttpResponse] = requests.get,
    timeout: float = HTTP_TIMEOUT,
    listing: str | None = None,
) -> list[pd.DataFrame]:
    """Fetch and parse one basin's NHC operational best-track files.

    Lists the ATCF `btk` directory (unless a `listing` is supplied), selects
    the storm files for `basin` in the requested `years`, fetches each, and
    folds it into a per-storm DataFrame. Resilient in the batch sense
    (matching the tropycal backend's season/storm policy): a listing that
    cannot be read, or a single storm file that fails to fetch or parse, is
    logged and skipped so the rest of the basin still returns.

    Args:
        basin: An earthlens basin code in `NHC_BTK_BASINS`
            (`"north_atlantic"` / `"east_pacific"` / `"central_pacific"`).
        years: The season years to include (e.g. `range(2026, 2027)`).
        base_url: The ATCF best-track directory URL (overridable in tests).
        get: The HTTP getter, `requests.get` by default; injected in tests.
            Called as `get(url, timeout=...)` and expected to return an
            object with `.text` and `.raise_for_status()`.
        timeout: Per-request timeout in seconds.
        listing: A directory index already fetched by `fetch_btk_listing`.
            When `None` (the default) this call fetches it itself; a caller
            serving several basins passes the shared index so the directory
            is listed only once.

    Returns:
        list[pd.DataFrame]: One folded best-track frame per storm that has
            at least one fix, in storm-number order. Empty when the basin
            has no files for the years, or the listing could not be read.

    Raises:
        KeyError: If `basin` is not an `NHC_BTK_BASINS` key (the backend
            validates this before calling, so it signals a programming
            error here).

    Examples:
        - Fetch one basin's season through the injectable getter (the same
          seam the tests use, so the example stays off the network):
            ```python
            >>> from earthlens.tropycal.nhc import fetch_btk_frames, BTK_BASE_URL
            >>> class _Resp:
            ...     def __init__(self, text):
            ...         self.text = text
            ...     def raise_for_status(self):
            ...         return None
            >>> row = "EP, 12, 2026090218, , BEST, 0, 133N, 1537W, 140, 927, HU, 64, NEQ, 30, 25, 20, 30"
            >>> pages = {
            ...     BTK_BASE_URL: _Resp('<a href="bep122026.dat">bep122026.dat</a>'),
            ...     BTK_BASE_URL + "bep122026.dat": _Resp(row),
            ... }
            >>> frames = fetch_btk_frames(
            ...     "east_pacific", [2026], get=lambda url, timeout=None: pages[url]
            ... )
            >>> frames[0]["id"].iloc[0]
            'EP122026'

            ```
    """
    prefix = NHC_BTK_BASINS[basin]
    wanted_years = {str(year) for year in years}
    if listing is None:
        listing = fetch_btk_listing(base_url=base_url, get=get, timeout=timeout)
    if listing is None:
        return []

    pattern = re.compile(_FILE_RE_TEMPLATE.format(prefix=prefix))
    # ATCF reserves cyclone numbers 90-99 for INVEST / genesis areas; those are
    # pre-genesis disturbances, not storms, so they are filtered out here rather
    # than surfaced as (unnamed) track features.
    matches = {
        match.group(0): (match.group(1), match.group(2))
        for match in pattern.finditer(listing)
        if match.group(2) in wanted_years and int(match.group(1)) < _INVEST_NUMBER
    }

    frames: list[pd.DataFrame] = []
    for name in sorted(matches):
        number, year = matches[name]
        storm_id = f"{prefix.upper()}{number}{year}"
        text = _get_text(
            f"{base_url}{name}", get=get, timeout=timeout, what=f"btk file {name}"
        )
        if text is None:
            continue
        try:
            frame = parse_btk_file(text, storm_id=storm_id, basin=basin)
        except Exception as exc:  # noqa: BLE001
            # Batch resilience (matches the fetch-failure path and the
            # tropycal backend's season/storm policy): a storm file that
            # cannot be parsed is logged with its exception type and skipped
            # so the rest of the basin still returns.
            logger.warning(
                f"NHC btk file {name} could not be parsed: {type(exc).__name__}: {exc}"
            )
            continue
        if len(frame):
            frames.append(frame)
    return frames


def parse_btk_file(text: str, *, storm_id: str, basin: str) -> pd.DataFrame:
    """Parse one ATCF `btk` file's text into a folded per-fix DataFrame.

    Keeps only `BEST` track rows, groups them by synoptic time, and folds
    the per-threshold wind-radii rows of a fix into one record: the track
    fields (`lat` / `lon` / `vmax` / `mslp` / `type`) are read from the
    first row of each fix (they repeat across its threshold rows), and each
    row's radii (fields 13-16, NE / SE / SW / NW) populate the
    `r{threshold}_{quadrant}` columns. A fix with only a `0`-threshold row
    (a weak storm) carries `NaN` radii.

    Args:
        text: The raw ATCF best-track file contents.
        storm_id: The storm identifier to stamp (e.g. `"EP122026"`),
            derived from the file name by `fetch_btk_frames`.
        basin: The earthlens basin code, stamped as the `wmo_basin` column
            so the events mapper fills the `basin` output field.

    Returns:
        pd.DataFrame: One row per fix (in file order), with the track-field
            columns, the twelve `RADII_COLUMNS`, and the `id` / `name` /
            `wmo_basin` attribute columns. Empty when the file has no usable
            `BEST` rows.

    Examples:
        - Fold a fix's 34- and 64-kt rows into one record and read its radii:
            ```python
            >>> from earthlens.tropycal.nhc import parse_btk_file
            >>> row34 = "EP, 12, 2026090218, , BEST, 0, 133N, 1537W, 140, 927, HU, 34, NEQ, 80, 60, 60, 80"
            >>> row64 = "EP, 12, 2026090218, , BEST, 0, 133N, 1537W, 140, 927, HU, 64, NEQ, 30, 25, 20, 30"
            >>> frame = parse_btk_file(
            ...     chr(10).join([row34, row64]), storm_id="EP122026", basin="east_pacific"
            ... )
            >>> len(frame)
            1
            >>> frame[["r34_ne", "r64_ne"]].iloc[0].tolist()
            [80.0, 30.0]

            ```
        - A weak (threshold-0) fix parses with its radii left as NaN:
            ```python
            >>> from earthlens.tropycal.nhc import parse_btk_file
            >>> row = "EP, 12, 2026082518, , BEST, 0, 101N, 1367W, 15, 1008, DB, 0, , 0, 0, 0, 0"
            >>> frame = parse_btk_file(row, storm_id="EP122026", basin="east_pacific")
            >>> round(float(frame["lon"].iloc[0]), 1)
            -136.7
            >>> bool(frame["r34_ne"].isna().iloc[0])
            True

            ```
    """
    fixes: dict[pd.Timestamp, dict[str, object]] = {}
    order: list[pd.Timestamp] = []
    name: str | None = None

    for line in text.splitlines():
        parts = [field.strip() for field in line.split(",")]
        if len(parts) < _MIN_FIELDS or parts[4] != "BEST":
            continue
        time = _parse_time(parts[2], parts[3])
        if time is None:
            continue

        record = fixes.get(time)
        if record is None:
            record = _new_fix_record(time, parts)
            fixes[time] = record
            order.append(time)

        candidate = parts[27] if len(parts) > 27 else ""
        if _is_storm_name(candidate):
            name = candidate

        threshold = _to_int(parts[11])
        if threshold in _RADII_THRESHOLD_SET:
            _fold_radii(record, threshold, parts)

    if not order:
        return pd.DataFrame()
    frame = pd.DataFrame([fixes[time] for time in order])
    frame["id"] = storm_id
    frame["name"] = name
    frame["wmo_basin"] = basin
    return frame


def _new_fix_record(time: pd.Timestamp, parts: list[str]) -> dict[str, object]:
    """Build a fresh per-fix record (track fields set, radii all `NaN`)."""
    record: dict[str, object] = {
        "time": time,
        "lat": _parse_latlon(parts[6]),
        "lon": _parse_latlon(parts[7]),
        "vmax": _to_int(parts[8]),
        "mslp": _to_int(parts[9]),
        "type": parts[10] or None,
    }
    for column in RADII_COLUMNS:
        record[column] = float("nan")
    return record


def _fold_radii(record: dict[str, object], threshold: int, parts: list[str]) -> None:
    """Fill a fix record's `r{threshold}_{quadrant}` columns from one row.

    Reads the four radii at fields 13-16 (NE / SE / SW / NW); a radius of 0
    or a missing value leaves the column at its `NaN` default so an absent
    quadrant is distinguishable from a measured zero.
    """
    for offset, quadrant in enumerate(RADII_QUADRANTS):
        radius = _to_int(parts[13 + offset])
        if radius is not None and radius > 0:
            record[f"r{threshold}_{quadrant}"] = float(radius)


def _get_text(
    url: str,
    *,
    get: Callable[..., _HttpResponse],
    timeout: float,
    what: str,
) -> str | None:
    """Return the text at `url`, or `None` (logged) when the fetch fails.

    Args:
        url: The URL to fetch.
        get: The HTTP getter (`requests.get` or a test double).
        timeout: Per-request timeout in seconds.
        what: A short label for the log line on failure.

    Returns:
        The response body text, or `None` when the request raised or
        returned an error status.
    """
    try:
        response = get(url, timeout=timeout)
        response.raise_for_status()
        return str(response.text)
    except Exception as exc:  # noqa: BLE001
        # Intentionally broad (batch resilience, matching the tropycal
        # backend's season/storm policy): a listing or a single storm file
        # that cannot be read is logged with its exception type and skipped
        # so the rest of the basin still returns.
        logger.warning(f"NHC {what} unavailable: {type(exc).__name__}: {exc}")
        return None


def _parse_time(token: str, minute_token: str = "") -> pd.Timestamp | None:
    """Parse an ATCF `YYYYMMDDHH` UTC timestamp plus the optional minute field.

    Field 2 carries the synoptic hour; for `BEST` tracks field 3 may carry the
    minute of a special (e.g. landfall / peak-intensity) fix, so folding on the
    hour alone would merge such a fix into the synoptic one. The minute is
    included in the timestamp when present and valid (`0 <= minute < 60`); a
    missing or malformed minute is treated as `00`. A malformed date token
    yields `None`.
    """
    token = token.strip()
    if len(token) != 10 or not token.isdigit():
        return None
    minute = _to_int(minute_token)
    if minute is None or not 0 <= minute < 60:
        minute = 0
    try:
        return pd.Timestamp(
            year=int(token[0:4]),
            month=int(token[4:6]),
            day=int(token[6:8]),
            hour=int(token[8:10]),
            minute=minute,
        )
    except ValueError:
        return None


def _parse_latlon(token: str) -> float | None:
    """Parse an ATCF lat/lon (`"133N"` → 13.3, `"1537W"` → -153.7).

    The value is tenths of a degree with an N/S/E/W suffix; S and W are
    negative. A missing or malformed token yields `None`.
    """
    token = token.strip()
    if len(token) < 2:
        return None
    hemisphere = token[-1].upper()
    if hemisphere not in ("N", "S", "E", "W"):
        return None
    magnitude = _to_int(token[:-1])
    if magnitude is None:
        return None
    value = magnitude / 10.0
    return -value if hemisphere in ("S", "W") else value


def _to_int(token: str) -> int | None:
    """Parse an ATCF integer field, mapping blank / non-numeric to `None`."""
    token = token.strip()
    if not token:
        return None
    try:
        return int(token)
    except ValueError:
        return None


def _is_storm_name(token: str) -> bool:
    """Return whether an ATCF field-27 token is a real storm name.

    A real name is alphabetic — hyphens allowed, so numbered-depression
    designations such as `TWO-E` / `SIX-E` register — and not a placeholder.
    The hyphens-aside alphabetic test rejects the numbered pre-naming labels
    (`GENESIS021`, which carry digits), and the placeholder set rejects
    `INVEST` / `UNNAMED`-style labels, so a storm's assigned name wins over
    its earlier disturbance label.
    """
    token = token.strip()
    if not token or not token.replace("-", "").isalpha():
        return False
    return token.upper() not in _NAME_PLACEHOLDERS
