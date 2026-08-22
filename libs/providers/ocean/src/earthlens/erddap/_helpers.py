"""Pure, stateless helpers for the ERDDAP backend.

No SDK and no network: these build the request shapes the backend hands
to erddapy (tabledap) or to a plain HTTP GET (griddap), so they are
unit-testable in isolation. The two request models differ — tabledap
uses an erddapy `constraints` dict, griddap uses an OPeNDAP-style URL
with `(value)` coordinate subsetting — so :func:`build_constraints` is
protocol-aware and :func:`build_griddap_url` assembles the griddap URL
directly. The exact spellings here were pinned against live erddapy
`3.2.1` in the A1 gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent, TemporalExtent

#: ISO-8601 stamp ERDDAP accepts for `time` constraints (UTC `Z`).
_ISO_TIME = "%Y-%m-%dT%H:%M:%SZ"

#: The two ERDDAP longitude subset-constraint keys (`>=` / `<=`).
_LON_GE = "longitude>="
_LON_LE = "longitude<="


def build_constraints(
    space: SpatialExtent,
    time: TemporalExtent,
    protocol: str,
    lon_360: bool = False,
) -> dict:
    """Map a bbox + time window onto an ERDDAP constraints dict.

    Both protocols share the `>=`/`<=` subset keys; **griddap**
    additionally needs a `{dim}_step` stride per axis (`1` = full
    resolution), which :func:`build_griddap_url` consumes when it builds
    the OPeNDAP `[(lo):step:(hi)]` ranges. tabledap passes the dict
    straight to `erddapy.ERDDAP.constraints`.

    The framework's :class:`~earthlens.base.SpatialExtent` always carries
    longitudes in the `[-180, 180]` convention. Some ERDDAP servers
    (notably the UHSLC tide-gauge tabledap datasets) store longitude in
    `[0, 360]` instead, so a raw `[-180, 180]` bound matches nothing.
    Pass `lon_360=True` for such a **tabledap** row to shift the two
    `longitude` keys into `[0, 360]`; a near-global or seam-crossing box
    (one straddling the prime meridian) drops the longitude constraint
    entirely — latitude + time still subset — and logs a warning so the
    caller knows the result is not longitude-bounded. The flag is ignored
    for griddap.

    Args:
        space: The request bbox (a :class:`~earthlens.base.SpatialExtent`).
        time: The request window (a :class:`~earthlens.base.TemporalExtent`).
        protocol: `"tabledap"` or `"griddap"`.
        lon_360: Shift the tabledap longitude bounds from `[-180, 180]`
            into the server's `[0, 360]` convention. No effect on griddap.

    Returns:
        dict: The constraints. For `"tabledap"`, the `time`/`latitude`/
            `longitude` `>=`/`<=` keys (the two `longitude` keys are
            dropped for a `lon_360` box that wraps or spans the globe);
            for `"griddap"`, the six keys plus `time_step`,
            `latitude_step`, `longitude_step` (all `1`).

    Raises:
        ValueError: If `protocol` is neither `"tabledap"` nor `"griddap"`.

    Examples:
        - tabledap yields the six subset keys:

            ```python
            >>> from types import SimpleNamespace
            >>> from datetime import datetime
            >>> space = SimpleNamespace(south=0.0, north=1.0, west=10.0, east=11.0)
            >>> time = SimpleNamespace(
            ...     start_date=datetime(2023, 1, 1), end_date=datetime(2023, 1, 2)
            ... )
            >>> c = build_constraints(space, time, "tabledap")
            >>> sorted(c)
            ['latitude<=', 'latitude>=', 'longitude<=', 'longitude>=', 'time<=', 'time>=']
            >>> c["time>="], c["latitude<="]
            ('2023-01-01T00:00:00Z', 1.0)

            ```
        - a `lon_360` box shifts a negative longitude into `[0, 360]`:

            ```python
            >>> sf = SimpleNamespace(south=37.0, north=38.5, west=-123.5, east=-121.5)
            >>> c = build_constraints(sf, time, "tabledap", lon_360=True)
            >>> c["longitude>="], c["longitude<="]
            (236.5, 238.5)

            ```
        - griddap adds the three `_step` strides:

            ```python
            >>> c = build_constraints(space, time, "griddap")
            >>> c["time_step"], c["latitude_step"], c["longitude_step"]
            (1, 1, 1)

            ```
    """
    if protocol not in ("tabledap", "griddap"):
        raise ValueError(f"protocol must be 'tabledap' or 'griddap', got {protocol!r}.")
    base = {
        "time>=": time.start_date.strftime(_ISO_TIME),
        "time<=": time.end_date.strftime(_ISO_TIME),
        "latitude>=": space.south,
        "latitude<=": space.north,
        _LON_GE: space.west,
        _LON_LE: space.east,
    }
    if protocol == "griddap":
        return {**base, "time_step": 1, "latitude_step": 1, "longitude_step": 1}
    if lon_360:
        west, east = space.west % 360.0, space.east % 360.0
        if (space.east - space.west) >= 359.0 or west > east:
            # Near-global or wraps the 0/360 seam (e.g. an AOI straddling the
            # prime meridian) — ERDDAP cannot express a wrapped `>=`/`<=` range,
            # so drop the longitude filter and let latitude + time subset the
            # stations. Warn loudly: the caller gets every latitude-matching
            # station, not just those in its longitude band.
            del base[_LON_GE], base[_LON_LE]
            logger.warning(
                f"ERDDAP lon_360: the requested longitude band "
                f"[{space.west}, {space.east}] wraps the 0/360 seam (or is "
                "near-global), which a single tabledap query cannot express; "
                "returning stations filtered by latitude + time only. Split the "
                "request at 0 deg (or 180 deg) for a longitude-bounded result."
            )
        else:
            base[_LON_GE], base[_LON_LE] = west, east
    return base


def build_griddap_url(
    server_url: str,
    dataset_id: str,
    variables: list[str],
    dim_names: list[str],
    constraints: dict,
) -> str:
    """Build the OPeNDAP griddap download URL for a `.nc` subset.

    Reproduces the exact format erddapy emits (verified byte-for-byte in
    the A1 gate) without touching the erddapy instance — whose
    `dataset_id` setter would auto-fetch the full coordinate axis from
    the server (a slow / hanging metadata call). Each variable is
    subset per dimension in `dim_names` order: a dimension with both
    `{dim}>=` and `{dim}<=` constraints becomes `[(lo):step:(hi)]`
    (`step` from `{dim}_step`, default `1`); a dimension without
    constraints becomes `[]` (its full range), which gracefully handles
    a single-level `altitude` / `depth` axis the bbox does not pin.

    Args:
        server_url: ERDDAP base URL (a trailing slash is tolerated).
        dataset_id: The griddap dataset id on that server.
        variables: Grid variables to request (at least one).
        dim_names: The grid's dimension order (e.g.
            `["time", "latitude", "longitude"]`).
        constraints: A :func:`build_constraints` griddap dict.

    Returns:
        str: The full `…/griddap/<id>.nc?<var>[…][…]…` download URL.

    Examples:
        - A time/lat/lon cube subsets every axis:

            ```python
            >>> url = build_griddap_url(
            ...     "https://example.org/erddap/",
            ...     "NOAA_DHW",
            ...     ["CRW_SSTANOMALY"],
            ...     ["time", "latitude", "longitude"],
            ...     {
            ...         "time>=": "2023-06-01T12:00:00Z",
            ...         "time<=": "2023-06-01T12:00:00Z",
            ...         "time_step": 1,
            ...         "latitude>=": 0.0, "latitude<=": 1.0, "latitude_step": 1,
            ...         "longitude>=": 150.0, "longitude<=": 151.0, "longitude_step": 1,
            ...     },
            ... )
            >>> url
            'https://example.org/erddap/griddap/NOAA_DHW.nc?CRW_SSTANOMALY[(2023-06-01T12:00:00Z):1:(2023-06-01T12:00:00Z)][(0.0):1:(1.0)][(150.0):1:(151.0)]'

            ```
    """
    base = f"{server_url.rstrip('/')}/griddap/{dataset_id}.nc?"
    encoded: list[str] = []
    for var in variables:
        sub = [var]
        for dim in dim_names:
            low = constraints.get(f"{dim}>=")
            high = constraints.get(f"{dim}<=")
            if low is None or high is None:
                sub.append("[]")
            else:
                step = constraints.get(f"{dim}_step", 1)
                sub.append(f"[({low}):{step}:({high})]")
        encoded.append("".join(sub))
    return base + ",".join(encoded)


def empty_canonical(columns: list[str]) -> pd.DataFrame:
    """Return a 0-row frame with exactly `columns` (the no-match fallback).

    Mirrors the usgs_water empty-frame contract: a tabledap query that
    matches no rows returns this so `download()` always hands back a
    `DataFrame` with the requested columns rather than raising.

    Args:
        columns: Column names for the empty frame.

    Returns:
        pd.DataFrame: A frame with `columns` and no rows.

    Examples:
        - Columns are preserved, length is zero:

            ```python
            >>> df = empty_canonical(["time", "sst"])
            >>> list(df.columns), len(df)
            (['time', 'sst'], 0)

            ```
    """
    return pd.DataFrame({column: [] for column in columns})
