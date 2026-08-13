"""Catalog-tooling handlers for the NEXRAD radar backend.

Registered with core's catalog-tooling commands through the `earthlens.cli`
entry-point group (see `earthlens._atmosphere_cli`). The refresher / writer parse
the public NOAA HOMR fixed-width station registry; the live validator lists the
unsigned NEXRAD chunk feed.
"""

from __future__ import annotations

from typing import Any

from earthlens.cli.toolkit import (
    BackendInfo,
    get_text,
    index_path,
    lint,
    replace_index_block,
    require,
)

#: NOAA HOMR registry of every WSR-88D / NEXRAD site (public, fixed-width).
_RADAR_STATIONS_URL = "https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt"


def _radar_column_spans(separator: str) -> list[tuple[int, int]]:
    """Return one `(start, end)` slice per dash-run column in the rule line."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, char in enumerate(separator):
        if char == "-" and start is None:
            start = index
        elif char != "-" and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(separator)))
    return spans


def _radar_station_rows(text: str) -> dict[str, dict[str, Any]]:
    """Parse the HOMR `nexrad-stations.txt` body into full station rows.

    The file is a fixed-width table: a header row, a row of dash runs
    marking each column's span, then one row per site. Keeps the four-letter
    alphabetic ICAO sites with in-range coordinates — the shape of the
    catalog's `stations:` block. Returns an empty mapping (rather than
    raising) when the table is too short or its header lacks any of the
    required `ICAO` / `NAME` / `LAT` / `LON` columns.

    Args:
        text: The full `nexrad-stations.txt` body.

    Returns:
        Mapping of ICAO id to `{name, latitude, longitude, state}`, sorted
        (`state` is `""` when the table carries no `ST` column).
    """
    lines = text.splitlines()
    if len(lines) < 3:
        return {}
    spans = _radar_column_spans(lines[1])
    columns = {lines[0][s:e].strip(): (s, e) for s, e in spans}
    # ICAO / NAME / LAT / LON are read unconditionally below; bail cleanly if
    # the upstream table ever drops one rather than raising a KeyError.
    if not {"ICAO", "NAME", "LAT", "LON"} <= set(columns):
        return {}

    def cell(row: str, name: str) -> str:
        """Return the stripped value of the fixed-width `name` column in `row`."""
        start, end = columns[name]
        return row[start:end].strip()

    rows: dict[str, dict[str, Any]] = {}
    for row in lines[2:]:
        icao = cell(row, "ICAO")
        if len(icao) != 4 or not icao.isalpha():
            continue
        try:
            lat = round(float(cell(row, "LAT")), 4)
            lon = round(float(cell(row, "LON")), 4)
        except (ValueError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        rows[icao] = {
            "name": cell(row, "NAME").title(),
            "latitude": lat,
            "longitude": lon,
            "state": cell(row, "ST") if "ST" in columns else "",
        }
    return dict(sorted(rows.items()))


def _radar_station_ids(text: str) -> list[str]:
    """Return the sorted ICAO ids from the HOMR table (id column only)."""
    return sorted(_radar_station_rows(text))


def refresher(catalog: Any) -> dict[str, list[str]]:
    """List every live NEXRAD ICAO id from the public NOAA HOMR registry.

    Args:
        catalog: The loaded radar `Catalog` (unused; the registry is fixed).

    Returns:
        A single-group mapping `{"radar": [sorted ICAO ids]}`.
    """
    return {"radar": _radar_station_ids(get_text(_RADAR_STATIONS_URL))}


def writer(info: BackendInfo, grouped: dict[str, list[str]]) -> str:
    """Regenerate radar's curated `stations:` block from NOAA HOMR.

    Unlike the `available_*` writers, this rewrites the *curated* station
    registry itself — re-parsing the HOMR table into full `{name, latitude,
    longitude, state}` rows (the radar catalog has no separate index; its
    `stations:` map is the catalog).

    Args:
        info: The radar backend.
        grouped: The live id fetch (unused; the full table is re-fetched).

    Returns:
        The path of the rewritten catalog file.
    """
    path = index_path(info)
    replace_index_block(
        path, "stations", _radar_station_rows(get_text(_RADAR_STATIONS_URL))
    )
    return str(path)


def validator(catalog: Any) -> tuple[int, list[str]]:
    """Each radar station needs a name and in-range latitude / longitude."""

    def check(key: str, record: Any) -> list[str]:
        """Flag a station missing a name or with out-of-range coordinates."""
        issues = require(key, record, ("name",))
        lat = getattr(record, "latitude", None)
        lon = getattr(record, "longitude", None)
        if not (isinstance(lat, (int, float)) and -90 <= lat <= 90):
            issues.append(f"{key}: latitude {lat!r} out of range")
        if not (isinstance(lon, (int, float)) and -180 <= lon <= 180):
            issues.append(f"{key}: longitude {lon!r} out of range")
        return issues

    return lint(catalog, check)


def _radar_feed_stations(region: str = "us-east-1") -> set[str]:
    """Return the station ids currently present in the NEXRAD chunk feed.

    Lists the top-level `{STATION}/` prefixes in the unsigned
    `unidata-nexrad-level2-chunks` bucket (the real-time feed
    `earthlens.radar` fetches from). Ported from the retired
    `tools/radar/audit_radar_catalog.py`.

    Args:
        region: AWS region of the bucket.

    Returns:
        The set of station-id prefixes currently in the feed.
    """
    from earthlens.radar.backend import BUCKET, _s3_client

    client = _s3_client(region)
    stations: set[str] = set()
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": BUCKET, "Delimiter": "/"}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        stations.update(
            prefix["Prefix"].rstrip("/")
            for prefix in response.get("CommonPrefixes", [])
        )
        token = response.get("NextContinuationToken")
        if not response.get("IsTruncated"):
            break
    return stations


def live_validator(catalog: Any) -> tuple[int, list[str]]:
    """Confirm the real-time NEXRAD chunk feed is reachable and lines up.

    Flags a hard failure (feed served nothing → unreachable / outage) or an
    id-format mismatch (the feed is non-empty but no catalogued station is in
    it). Per-station idleness is expected — the feed is a rolling ~1-2 h
    buffer — so it is not flagged.
    """
    catalogued = set(catalog.datasets)
    feed = _radar_feed_stations()
    if not feed:
        return len(catalogued), [
            "NEXRAD chunk feed served no stations (unreachable / outage?)"
        ]
    if not (catalogued & feed):
        return len(catalogued), [
            "no catalogued station is in the live feed "
            "(id format may not match the feed prefixes)"
        ]
    return len(catalogued), []
