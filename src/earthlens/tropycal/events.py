"""Map tropycal storm track frames into pyramids `FeatureCollection`s.

This module is the only place in the Tropycal backend that touches a GIS
vector container, so per the pyramids policy it keeps all geometry/CRS
handling inside pyramids primitives: earthlens assembles the plain
attribute rows from each storm's `Storm.to_dataframe(attrs_as_columns=
True)` frame, builds the geometry column (one `Point` per 6-hourly fix,
or one `LineString` per storm), and hands the result to
:class:`pyramids.feature.collection.FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) tagged `EPSG:4326`.

Two output geometries are supported (the `geometry=` backend kwarg):

* **point** (default) — one row per track fix, schema
  :data:`POINT_COLUMNS`.
* **track** — one row per storm, the whole path as a `LineString`,
  schema :data:`TRACK_COLUMNS`, with per-storm summary attributes
  (max wind, min pressure, max category, ACE, start/end time).

Filtering is fix-level and "loose" (`G4`): a fix is kept when its time is
inside `[start, end]` **and** its position is inside the bbox. In point
mode a storm contributes only its in-window, in-box fixes. In track mode
a storm is included when **any** fix falls in the window+bbox, and the
rendered `LineString` is built from its **in-window** fixes (so the drawn
track is clipped to the time window, not the bbox).

Tropycal's `to_dataframe()` exposes no Saffir-Simpson `category` column,
so it is derived from `vmax` here via :func:`saffir_simpson_category`
(the SSHWS wind thresholds, in knots). This keeps the module free of any
`tropycal` import, so unit tests can drive it with hand-built frames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import LineString, Point

if TYPE_CHECKING:
    import datetime as dt

Geometry = Literal["point", "track"]

#: WGS84 — the CRS every Tropycal FeatureCollection is tagged with.
EVENT_CRS = "EPSG:4326"

#: Per-fix point-mode schema (attribute columns + dtypes). The `geometry`
#: column (a `shapely.Point` per row) is added separately and not listed.
POINT_COLUMNS: dict[str, str] = {
    "storm_id": "string",
    "name": "string",
    "time": "datetime64[ns, UTC]",
    "lat": "float64",
    "lon": "float64",
    "vmax_kt": "float64",
    "mslp_hpa": "float64",
    "storm_type": "string",
    "category": "int64",
    "basin": "string",
    "source": "string",
}

#: Per-storm track-mode schema. The `geometry` column (a
#: `shapely.LineString` per row) is added separately and not listed.
TRACK_COLUMNS: dict[str, str] = {
    "storm_id": "string",
    "name": "string",
    "basin": "string",
    "source": "string",
    "start_time": "datetime64[ns, UTC]",
    "end_time": "datetime64[ns, UTC]",
    "max_vmax_kt": "float64",
    "min_mslp_hpa": "float64",
    "max_category": "int64",
    "ace": "float64",
}

#: Saffir-Simpson upper wind bounds (knots) for categories 0..4; a wind
#: above the last bound is category 5. Index = category.
_SSHWS_UPPER_KT = (63, 82, 95, 112, 136)


def saffir_simpson_category(vmax_kt: float | int | None) -> int:
    """Return the Saffir-Simpson category for a max-wind value in knots.

    Tropycal's `to_dataframe()` has no `category` column, so the backend
    derives it from `vmax`. Sub-hurricane intensities (tropical
    depression / tropical storm, `< 64` kt) map to `0`; categories 1-5
    follow the standard SSHWS knot thresholds. A missing wind (`None` /
    `NaN`) maps to `0`.

    Args:
        vmax_kt: Maximum sustained wind in knots, or `None`/`NaN`.

    Returns:
        int: Saffir-Simpson category in `0..5`.

    Examples:
        - Thresholds at the category boundaries:
            ```python
            >>> from earthlens.tropycal.events import saffir_simpson_category
            >>> saffir_simpson_category(30)
            0
            >>> saffir_simpson_category(64)
            1
            >>> saffir_simpson_category(140)
            5
            >>> saffir_simpson_category(None)
            0

            ```
    """
    if vmax_kt is None or pd.isna(vmax_kt):
        return 0
    wind = float(vmax_kt)
    for category, upper in enumerate(_SSHWS_UPPER_KT):
        if wind <= upper:
            return category
    return 5


def frame_to_fc(
    storm_frames: Iterable[pd.DataFrame],
    *,
    geometry: Geometry,
    window: tuple[dt.datetime, dt.datetime],
    bbox: tuple[float, float, float, float],
    source: str,
) -> FeatureCollection:
    """Map per-storm track frames to a `FeatureCollection`.

    Each input frame is one storm's `Storm.to_dataframe(attrs_as_columns=
    True)` output (so it carries `id` / `name` alongside the per-fix
    `time` / `lat` / `lon` / `vmax` / `mslp` / `type` columns). Fixes are
    filtered to the window + bbox per :mod:`earthlens.tropycal.events`
    module docs, then mapped to the chosen geometry.

    Args:
        storm_frames: Iterable of per-storm DataFrames.
        geometry: `"point"` (one row per in-window/in-box fix) or
            `"track"` (one `LineString` row per included storm).
        window: `(start, end)` inclusive datetime bounds for the
            fix-level time filter (tz-naive, matching tropycal's
            `time` column).
        bbox: `(south, north, west, east)` degree bounds for the
            fix-level position filter.
        source: The requested data source (`"ibtracs"`/`"hurdat"`),
            stamped on every row.

    Returns:
        FeatureCollection: Point- or track-mode collection, CRS
            `EPSG:4326`. Empty (schema-only) when nothing matched.

    Examples:
        - Map two in-window fixes to point features and read a column:
            ```python
            >>> import datetime as dt
            >>> import pandas as pd
            >>> from earthlens.tropycal.events import frame_to_fc
            >>> frame = pd.DataFrame({
            ...     "time": pd.to_datetime(["2005-08-25", "2005-08-26"]),
            ...     "lat": [25.0, 26.0], "lon": [-85.0, -86.0],
            ...     "vmax": [70.0, 100.0], "mslp": [990.0, 950.0],
            ...     "type": ["HU", "HU"], "wmo_basin": ["north_atlantic"] * 2,
            ...     "id": ["AL122005"] * 2, "name": ["KATRINA"] * 2, "ace": [20.0, 20.0],
            ... })
            >>> fc = frame_to_fc(
            ...     [frame], geometry="point",
            ...     window=(dt.datetime(2005, 8, 1), dt.datetime(2005, 9, 1)),
            ...     bbox=(18.0, 31.0, -98.0, -80.0), source="hurdat",
            ... )
            >>> len(fc)
            2
            >>> list(fc["category"])
            [1, 3]

            ```
        - A storm wholly outside the window yields an empty collection:
            ```python
            >>> import datetime as dt
            >>> import pandas as pd
            >>> from earthlens.tropycal.events import frame_to_fc
            >>> frame = pd.DataFrame({
            ...     "time": pd.to_datetime(["2010-01-01", "2010-01-02"]),
            ...     "lat": [25.0, 26.0], "lon": [-85.0, -86.0],
            ...     "vmax": [70.0, 100.0], "mslp": [990.0, 950.0],
            ...     "type": ["HU", "HU"], "wmo_basin": ["north_atlantic"] * 2,
            ...     "id": ["AL122005"] * 2, "name": ["KATRINA"] * 2, "ace": [20.0, 20.0],
            ... })
            >>> fc = frame_to_fc(
            ...     [frame], geometry="point",
            ...     window=(dt.datetime(2005, 8, 1), dt.datetime(2005, 9, 1)),
            ...     bbox=(18.0, 31.0, -98.0, -80.0), source="hurdat",
            ... )
            >>> len(fc)
            0

            ```
    """
    south, north, west, east = bbox
    start, end = window
    rows: list[dict[str, object]] = []
    geoms: list[object] = []

    for frame in storm_frames:
        if frame is None or len(frame) == 0:
            continue
        prepared = _prepare_frame(frame)
        in_window = prepared[
            (prepared["_time"] >= pd.Timestamp(start))
            & (prepared["_time"] <= pd.Timestamp(end))
        ]
        if in_window.empty:
            continue
        in_box = in_window[
            (in_window["_lat"] >= south)
            & (in_window["_lat"] <= north)
            & (in_window["_lon"] >= west)
            & (in_window["_lon"] <= east)
        ]
        if in_box.empty:
            continue

        if geometry == "track":
            track_row = _track_row(in_window, source)
            if track_row is not None:
                rows.append(track_row[0])
                geoms.append(track_row[1])
        else:
            for point_row, point in _point_rows(in_box, source):
                rows.append(point_row)
                geoms.append(point)

    columns = POINT_COLUMNS if geometry == "point" else TRACK_COLUMNS
    if not rows:
        return empty_fc(geometry)
    return _build_fc(rows, geoms, columns)


def empty_fc(geometry: Geometry = "point") -> FeatureCollection:
    """Return an empty `FeatureCollection` with the chosen-mode schema.

    Used for a query that matched no fixes/storms so callers always get
    the same columns and dtypes back regardless of hit count — an empty
    result is a legitimate answer, not an error.

    Args:
        geometry: `"point"` (:data:`POINT_COLUMNS`) or `"track"`
            (:data:`TRACK_COLUMNS`).

    Returns:
        FeatureCollection: Zero rows, the mode's columns with their
            declared dtypes, an empty `geometry` column, CRS
            `EPSG:4326`.

    Examples:
        - The point schema is present even with no rows:
            ```python
            >>> from earthlens.tropycal.events import empty_fc, POINT_COLUMNS
            >>> fc = empty_fc("point")
            >>> len(fc)
            0
            >>> set(POINT_COLUMNS).issubset(fc.columns)
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    columns = POINT_COLUMNS if geometry == "point" else TRACK_COLUMNS
    frame = pd.DataFrame(
        {column: pd.Series([], dtype=dtype) for column, dtype in columns.items()}
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=EVENT_CRS), crs=EVENT_CRS
    )
    return FeatureCollection(gdf)


def concat_fcs(
    collections: list[FeatureCollection], geometry: Geometry = "point"
) -> FeatureCollection:
    """Concatenate per-basin FeatureCollections into one.

    Skips empty inputs; returns :func:`empty_fc` when every input is
    empty (or the list is empty) so the result is always a
    schema-correct FeatureCollection.

    Args:
        collections: Per-basin FeatureCollections, each produced by
            :func:`frame_to_fc` in the same geometry mode (so all share
            the schema and CRS).
        geometry: The mode the inputs were built in, used to pick the
            empty-result schema when every input is empty.

    Returns:
        FeatureCollection: The row-wise union, CRS `EPSG:4326`.
    """
    non_empty = [fc for fc in collections if len(fc)]
    if not non_empty:
        return empty_fc(geometry)
    merged = pd.concat(non_empty, ignore_index=True)
    return FeatureCollection(
        gpd.GeoDataFrame(merged, geometry="geometry", crs=EVENT_CRS)
    )


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Add normalised helper columns (`_time`, `_lat`, `_lon`) to a frame.

    Reads the source columns defensively so a frame missing a column
    degrades to NaT/NaN rather than raising. `_time` is tz-naive (matching
    tropycal's `time`) for window comparison.

    Args:
        frame: One storm's `to_dataframe(attrs_as_columns=True)` output.

    Returns:
        A copy of `frame` with `_time` / `_lat` / `_lon` helper columns.
    """
    prepared = frame.copy()
    time_col = "time" if "time" in prepared.columns else "date"
    prepared["_time"] = pd.to_datetime(
        prepared.get(time_col), errors="coerce"
    ).dt.tz_localize(None)
    prepared["_lat"] = pd.to_numeric(prepared.get("lat"), errors="coerce")
    prepared["_lon"] = pd.to_numeric(prepared.get("lon"), errors="coerce")
    return prepared


def _point_rows(
    in_box: pd.DataFrame, source: str
) -> list[tuple[dict[str, object], object]]:
    """Build per-fix point rows from a storm's in-window/in-box fixes.

    Args:
        in_box: The storm's fixes already filtered to window + bbox.
        source: The requested data source, stamped on each row.

    Returns:
        A list of `(row, Point)` pairs, one per fix.
    """
    out: list[tuple[dict[str, object], object]] = []
    for _, fix in in_box.iterrows():
        vmax = _num(fix.get("vmax"))
        lon, lat = float(fix["_lon"]), float(fix["_lat"])
        row = {
            "storm_id": _str(fix.get("id")),
            "name": _str(fix.get("name")),
            "time": fix["_time"],
            "lat": lat,
            "lon": lon,
            "vmax_kt": vmax,
            "mslp_hpa": _num(fix.get("mslp")),
            "storm_type": _str(fix.get("type")),
            "category": saffir_simpson_category(vmax),
            "basin": _str(fix.get("wmo_basin")),
            "source": source,
        }
        out.append((row, Point(lon, lat)))
    return out


def _track_row(
    in_window: pd.DataFrame, source: str
) -> tuple[dict[str, object], object] | None:
    """Build one per-storm track row from a storm's in-window fixes.

    The `LineString` is built from the in-window fixes with a valid
    position; a storm with fewer than two such fixes cannot form a line
    and is skipped (returns `None`). Summary attributes are computed over
    the in-window fixes.

    Args:
        in_window: The storm's fixes filtered to the time window.
        source: The requested data source, stamped on the row.

    Returns:
        A `(row, LineString)` pair, or `None` when the storm has fewer
        than two positioned in-window fixes.
    """
    positioned = in_window.dropna(subset=["_lat", "_lon"])
    if len(positioned) < 2:
        return None
    coords = list(zip(positioned["_lon"].astype(float), positioned["_lat"].astype(float)))
    vmax = pd.to_numeric(in_window.get("vmax"), errors="coerce")
    mslp = pd.to_numeric(in_window.get("mslp"), errors="coerce")
    max_vmax = float(vmax.max()) if vmax.notna().any() else float("nan")
    ace = in_window.get("ace")
    row = {
        "storm_id": _str(_first(in_window.get("id"))),
        "name": _str(_first(in_window.get("name"))),
        "basin": _str(_first(in_window.get("wmo_basin"))),
        "source": source,
        "start_time": positioned["_time"].min(),
        "end_time": positioned["_time"].max(),
        "max_vmax_kt": max_vmax,
        "min_mslp_hpa": float(mslp.min()) if mslp.notna().any() else float("nan"),
        "max_category": saffir_simpson_category(max_vmax),
        "ace": _num(_first(ace)) if ace is not None else float("nan"),
    }
    return row, LineString(coords)


def _build_fc(
    rows: list[dict[str, object]],
    geoms: list[object],
    columns: dict[str, str],
) -> FeatureCollection:
    """Assemble typed rows + geometries into a `FeatureCollection`.

    Args:
        rows: Attribute rows keyed by `columns`.
        geoms: One shapely geometry per row (parallel to `rows`).
        columns: The mode schema (column -> pandas dtype).

    Returns:
        FeatureCollection: Typed collection, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame(rows, columns=list(columns))
    for column, dtype in columns.items():
        if dtype.startswith("datetime64"):
            frame[column] = pd.to_datetime(frame[column], utc=True).astype(dtype)
        else:
            frame[column] = frame[column].astype(dtype)
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries(geoms, crs=EVENT_CRS), crs=EVENT_CRS
    )
    return FeatureCollection(gdf)


def _num(value: object) -> float:
    """Coerce a value to float, mapping missing/non-numeric to `NaN`."""
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) else float("nan")


def _str(value: object) -> object:
    """Return `value` as-is unless it is missing, in which case `None`."""
    return value if value is not None and not pd.isna(value) else None


def _first(series: object) -> object:
    """Return the first element of a Series/sequence, or `None` if empty."""
    if series is None:
        return None
    if isinstance(series, pd.Series):
        return series.iloc[0] if len(series) else None
    return series
