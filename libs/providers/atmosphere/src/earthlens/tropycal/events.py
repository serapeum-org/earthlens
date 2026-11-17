"""Map tropycal storm track frames into pyramids `FeatureCollection`s.

This module is the only place in the Tropycal backend that touches a GIS
vector container, so per the pyramids policy it keeps all geometry/CRS
handling inside pyramids primitives: earthlens assembles the plain
attribute rows from each storm's `Storm.to_dataframe(attrs_as_columns=
True)` frame, builds the geometry column (one `Point` per 6-hourly fix,
or one `LineString` per storm), and hands the result to
`pyramids.feature.collection.FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) tagged `EPSG:4326`.

Two output geometries are supported (the `geometry=` backend kwarg):

* **point** (default) — one row per track fix, schema
  `POINT_COLUMNS`.
* **track** — one row per storm, the whole path as a `LineString`,
  schema `TRACK_COLUMNS`, with per-storm summary attributes
  (max wind, min pressure, max category, ACE, start/end time).

Filtering is fix-level and "loose" (`G4`): a fix is kept when its time is
inside `[start, end]` **and** its position is inside the bbox. In point
mode a storm contributes only its in-window, in-box fixes. In track mode
a storm is included when **any** fix falls in the window+bbox, and the
rendered `LineString` is built from its **in-window** fixes (so the drawn
track is clipped to the time window, not the bbox).

Tropycal's `to_dataframe()` exposes no Saffir-Simpson `category` column,
so it is derived from `vmax` here via `saffir_simpson_category`
(the SSHWS wind thresholds, in knots). This keeps the module free of any
`tropycal` import, so unit tests can drive it with hand-built frames.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Literal

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import LineString

if TYPE_CHECKING:
    import datetime as dt

Geometry = Literal["point", "track"]

#: WGS84 — the CRS every Tropycal FeatureCollection is tagged with.
EVENT_CRS = "EPSG:4326"

#: Per-fix point-mode schema (attribute columns + dtypes). The `geometry`
#: column (a `shapely.Point` per row) is added separately and not listed.
#: `vmax_kt` / `mslp_hpa` are `float64` (not `int`) even though tropycal emits
#: integer winds/pressures, so a missing value degrades to `NaN` rather than
#: forcing a lossy/again-failing integer cast.
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

#: Recon-observation point schema (`product="recon"`). One row per aircraft
#: observation; the `geometry` Point is added separately. Met fields are
#: float64 (NaN-safe) and read defensively — `dropsondes`/`vdms` frames omit
#: some of the `hdobs` columns.
RECON_COLUMNS: dict[str, str] = {
    "storm_id": "string",
    "recon_product": "string",
    "source": "string",
    "time": "datetime64[ns, UTC]",
    "lat": "float64",
    "lon": "float64",
    "wspd_kt": "float64",
    "pres_hpa": "float64",
    "temp_c": "float64",
}

#: Wind-radius thresholds (kt) carried by ATCF best-track rows; a `0`
#: threshold row carries no radii. Used by the NHC operational best-track
#: product (`product="nhc_btk"`).
RADII_THRESHOLDS: tuple[int, ...] = (34, 50, 64)

#: Quadrant suffixes, in the NE / SE / SW / NW order ATCF lists the four
#: wind-radius values of each threshold.
RADII_QUADRANTS: tuple[str, ...] = ("ne", "se", "sw", "nw")

#: The wind-radii output columns (`r34_ne` … `r64_nw`), all in nautical
#: miles — one per (threshold, quadrant) pair, in threshold-then-quadrant
#: order.
RADII_COLUMNS: tuple[str, ...] = tuple(
    f"r{threshold}_{quadrant}"
    for threshold in RADII_THRESHOLDS
    for quadrant in RADII_QUADRANTS
)

#: NHC operational best-track (`product="nhc_btk"`) point schema: the
#: per-fix `POINT_COLUMNS` plus the twelve wind-radii columns. Radii are
#: `float64` so an absent threshold degrades to `NaN`.
BTK_POINT_COLUMNS: dict[str, str] = {
    **POINT_COLUMNS,
    **dict.fromkeys(RADII_COLUMNS, "float64"),
}


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
    filtered to the window + bbox per `earthlens.tropycal.events`
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
    track_rows: list[dict[str, object]] = []
    track_geoms: list[object] = []
    point_frames: list[pd.DataFrame] = []

    for in_window, in_box in _iter_windowed_fixes(storm_frames, window, bbox):
        if geometry == "track":
            track_row = _track_row(in_window, source)
            if track_row is not None:
                track_rows.append(track_row[0])
                track_geoms.append(track_row[1])
        else:
            point_frames.append(in_box)

    if geometry == "track":
        if not track_rows:
            return empty_fc("track")
        return _build_fc(track_rows, track_geoms, TRACK_COLUMNS)

    if not point_frames:
        return empty_fc("point")
    return _points_to_fc(pd.concat(point_frames, ignore_index=True), source)


def btk_frame_to_fc(
    storm_frames: Iterable[pd.DataFrame],
    *,
    window: tuple[dt.datetime, dt.datetime],
    bbox: tuple[float, float, float, float],
    source: str,
) -> FeatureCollection:
    """Map NHC operational best-track frames to a point `FeatureCollection`.

    The point analogue of `frame_to_fc` for the `product="nhc_btk"`
    source: each input frame is one storm's folded ATCF best-track table
    (from `earthlens.tropycal.nhc.parse_btk_file`), carrying the per-fix
    `time` / `lat` / `lon` / `vmax` / `mslp` / `type` columns alongside the
    twelve `r34_ne` … `r64_nw` wind-radii columns. Fixes are filtered to
    the window + bbox with the same loose rule as best tracks, then mapped
    to the `BTK_POINT_COLUMNS` schema (one row per in-window/in-box fix).

    Args:
        storm_frames: Iterable of per-storm folded best-track DataFrames.
        window: `(start, end)` inclusive datetime bounds (tz-naive).
        bbox: `(south, north, west, east)` degree bounds.
        source: The source label stamped on every row (`"nhc"`).

    Returns:
        FeatureCollection: Point collection with the radii columns, CRS
            `EPSG:4326`. Empty (schema-only) when nothing matched.

    Examples:
        - Map one fix carrying 64-kt radii and read a radius column:
            ```python
            >>> import datetime as dt
            >>> import pandas as pd
            >>> from earthlens.tropycal.events import btk_frame_to_fc
            >>> frame = pd.DataFrame({
            ...     "id": ["EP122026"], "name": ["LOWELL"],
            ...     "time": pd.to_datetime(["2026-09-02 18:00"]),
            ...     "lat": [13.3], "lon": [-153.7], "vmax": [140.0],
            ...     "mslp": [927.0], "type": ["HU"], "wmo_basin": ["east_pacific"],
            ...     "r64_ne": [30.0], "r64_se": [25.0], "r64_sw": [20.0], "r64_nw": [30.0],
            ... })
            >>> fc = btk_frame_to_fc(
            ...     [frame],
            ...     window=(dt.datetime(2026, 8, 25), dt.datetime(2026, 9, 11)),
            ...     bbox=(-5.0, 40.0, -178.0, -75.0), source="nhc",
            ... )
            >>> len(fc)
            1
            >>> float(fc["r64_ne"].iloc[0])
            30.0

            ```
    """
    point_frames = [
        in_box for _, in_box in _iter_windowed_fixes(storm_frames, window, bbox)
    ]
    if not point_frames:
        return empty_btk_fc()
    return _btk_points_to_fc(pd.concat(point_frames, ignore_index=True), source)


def empty_fc(geometry: Geometry = "point") -> FeatureCollection:
    """Return an empty `FeatureCollection` with the chosen-mode schema.

    Used for a query that matched no fixes/storms so callers always get
    the same columns and dtypes back regardless of hit count — an empty
    result is a legitimate answer, not an error.

    Args:
        geometry: `"point"` (`POINT_COLUMNS`) or `"track"`
            (`TRACK_COLUMNS`).

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
    return _empty_typed_fc(columns)


def empty_btk_fc() -> FeatureCollection:
    """Return an empty NHC best-track `FeatureCollection` (schema-correct).

    The `product="nhc_btk"` empty-result counterpart of `empty_fc`, so a
    request that matched no fixes still returns the full `BTK_POINT_COLUMNS`
    schema (track fields + the twelve radii columns).

    Returns:
        FeatureCollection: Zero rows, the `BTK_POINT_COLUMNS` columns with
            their dtypes, an empty `geometry`, CRS `EPSG:4326`.

    Examples:
        - The radii columns are present even with no rows:
            ```python
            >>> from earthlens.tropycal.events import empty_btk_fc, RADII_COLUMNS
            >>> fc = empty_btk_fc()
            >>> len(fc)
            0
            >>> set(RADII_COLUMNS).issubset(fc.columns)
            True

            ```
    """
    return _empty_typed_fc(BTK_POINT_COLUMNS)


def concat_fcs(
    collections: list[FeatureCollection], geometry: Geometry = "point"
) -> FeatureCollection:
    """Concatenate per-basin FeatureCollections into one.

    Skips empty inputs; returns `empty_fc` when every input is
    empty (or the list is empty) so the result is always a
    schema-correct FeatureCollection.

    Args:
        collections: Per-basin FeatureCollections, each produced by
            `frame_to_fc` in the same geometry mode (so all share
            the schema and CRS).
        geometry: The mode the inputs were built in, used to pick the
            empty-result schema when every input is empty.

    Returns:
        FeatureCollection: The row-wise union, CRS `EPSG:4326`.
    """
    return _concat_typed(collections, lambda: empty_fc(geometry))


def recon_to_fc(
    frame: pd.DataFrame | None,
    *,
    storm_id: str,
    recon_product: str,
    window: tuple[dt.datetime, dt.datetime],
    bbox: tuple[float, float, float, float],
    source: str,
) -> FeatureCollection:
    """Map one storm's recon observation frame to a point `FeatureCollection`.

    `frame` is a `tropycal.recon` sub-product `.data` DataFrame (`hdobs`,
    `dropsondes`, or `vdms`); all carry `time` / `lat` / `lon`, while the met
    columns (`wspd`, `p_sfc`, `temp`) are read defensively since they differ
    by sub-product. Observations are filtered to the window + bbox at the
    point level (same loose rule as best-track fixes) and mapped to the
    `RECON_COLUMNS` schema. A `None`/empty frame (a storm with no recon
    data — most storms) returns `empty_recon_fc`.

    Args:
        frame: The recon sub-product `.data` frame, or `None`.
        storm_id: The storm identifier, stamped on every row.
        recon_product: `"hdobs"` / `"dropsondes"` / `"vdms"`, stamped on
            every row.
        window: `(start, end)` inclusive datetime bounds (tz-naive).
        bbox: `(south, north, west, east)` degree bounds.
        source: The data source used to resolve the storm.

    Returns:
        FeatureCollection: Recon observation points, CRS `EPSG:4326`.

    Examples:
        - Map two in-window/in-box observations and read a met column:
            ```python
            >>> import datetime as dt
            >>> import pandas as pd
            >>> from earthlens.tropycal.events import recon_to_fc
            >>> frame = pd.DataFrame({
            ...     "time": pd.to_datetime(["2005-08-28 12:00", "2005-08-28 12:10"]),
            ...     "lat": [25.0, 25.1], "lon": [-85.0, -85.1],
            ...     "wspd": [120.0, 125.0], "p_sfc": [945.0, 942.0], "temp": [22.0, 22.5],
            ... })
            >>> fc = recon_to_fc(
            ...     frame, storm_id="AL122005", recon_product="hdobs",
            ...     window=(dt.datetime(2005, 8, 1), dt.datetime(2005, 9, 1)),
            ...     bbox=(18.0, 31.0, -98.0, -80.0), source="hurdat",
            ... )
            >>> len(fc)
            2
            >>> list(fc["wspd_kt"])
            [120.0, 125.0]

            ```
    """
    if frame is None or len(frame) == 0:
        return empty_recon_fc()
    prepared = _prepare_frame(frame)
    south, north, west, east = bbox
    start, end = window
    kept = prepared[
        (prepared["_time"] >= pd.Timestamp(start))
        & (prepared["_time"] <= pd.Timestamp(end))
        & (prepared["_lat"] >= south)
        & (prepared["_lat"] <= north)
        & (prepared["_lon"] >= west)
        & (prepared["_lon"] <= east)
    ]
    if kept.empty:
        return empty_recon_fc()
    typed = pd.DataFrame(
        {
            "storm_id": storm_id,
            "recon_product": recon_product,
            "source": source,
            "time": kept["_time"].to_numpy(),
            "lat": kept["_lat"].to_numpy(),
            "lon": kept["_lon"].to_numpy(),
            "wspd_kt": pd.to_numeric(_column(kept, "wspd"), errors="coerce").to_numpy(),
            "pres_hpa": pd.to_numeric(
                _column(kept, "p_sfc"), errors="coerce"
            ).to_numpy(),
            "temp_c": pd.to_numeric(_column(kept, "temp"), errors="coerce").to_numpy(),
        }
    )
    geoms = gpd.points_from_xy(typed["lon"], typed["lat"])
    return _frame_to_typed_fc(typed, geoms, RECON_COLUMNS)


def empty_recon_fc() -> FeatureCollection:
    """Return an empty recon-observation `FeatureCollection` (schema-correct).

    Returns:
        FeatureCollection: Zero rows, the `RECON_COLUMNS` columns with
            their dtypes, an empty `geometry`, CRS `EPSG:4326`.

    Examples:
        - The recon schema is present even with no rows:
            ```python
            >>> from earthlens.tropycal.events import empty_recon_fc, RECON_COLUMNS
            >>> fc = empty_recon_fc()
            >>> len(fc)
            0
            >>> set(RECON_COLUMNS).issubset(fc.columns)
            True

            ```
    """
    return _empty_typed_fc(RECON_COLUMNS)


def concat_recon_fcs(collections: list[FeatureCollection]) -> FeatureCollection:
    """Row-union per-storm recon collections, falling back to `empty_recon_fc`."""
    return _concat_typed(collections, empty_recon_fc)


def concat_btk_fcs(collections: list[FeatureCollection]) -> FeatureCollection:
    """Row-union per-basin NHC best-track collections, else `empty_btk_fc`.

    Args:
        collections: Per-basin point collections from `btk_frame_to_fc`,
            all sharing the `BTK_POINT_COLUMNS` schema and CRS.

    Returns:
        FeatureCollection: The row-wise union, CRS `EPSG:4326`. A
            schema-correct empty collection when every input is empty.

    Examples:
        - Union two basins' fixes into one collection:
            ```python
            >>> import datetime as dt
            >>> import pandas as pd
            >>> from earthlens.tropycal.events import btk_frame_to_fc, concat_btk_fcs
            >>> win = (dt.datetime(2026, 8, 1), dt.datetime(2026, 10, 1))
            >>> box = (-5.0, 60.0, -178.0, -10.0)
            >>> ep = pd.DataFrame({
            ...     "id": ["EP122026"], "name": ["LOWELL"],
            ...     "time": pd.to_datetime(["2026-09-02 18:00"]),
            ...     "lat": [13.3], "lon": [-153.7], "vmax": [140.0],
            ...     "mslp": [927.0], "type": ["HU"], "wmo_basin": ["east_pacific"],
            ... })
            >>> al = ep.assign(id="AL012026", lon=-80.0, wmo_basin="north_atlantic")
            >>> a = btk_frame_to_fc([ep], window=win, bbox=box, source="nhc")
            >>> b = btk_frame_to_fc([al], window=win, bbox=box, source="nhc")
            >>> merged = concat_btk_fcs([a, b])
            >>> sorted(merged["storm_id"].tolist())
            ['AL012026', 'EP122026']

            ```
        - Only-empty inputs give back the radii schema, not an error:
            ```python
            >>> from earthlens.tropycal.events import concat_btk_fcs, empty_btk_fc, RADII_COLUMNS
            >>> merged = concat_btk_fcs([empty_btk_fc()])
            >>> len(merged)
            0
            >>> set(RADII_COLUMNS).issubset(merged.columns)
            True

            ```
    """
    return _concat_typed(collections, empty_btk_fc)


def _iter_windowed_fixes(
    storm_frames: Iterable[pd.DataFrame],
    window: tuple[dt.datetime, dt.datetime],
    bbox: tuple[float, float, float, float],
) -> Iterable[tuple[pd.DataFrame, pd.DataFrame]]:
    """Yield `(in_window, in_box)` for each storm passing both filters.

    The single implementation of the loose fix-level filter shared by the
    best-track (`frame_to_fc`) and NHC operational best-track
    (`btk_frame_to_fc`) point/track mappers: each storm frame is
    normalised (`_prepare_frame`), restricted to the `[start, end]` time
    window, then to the `(south, north, west, east)` bbox. A storm with no
    in-window or no in-box fix is skipped entirely.

    Args:
        storm_frames: Iterable of per-storm DataFrames.
        window: `(start, end)` inclusive datetime bounds (tz-naive).
        bbox: `(south, north, west, east)` degree bounds.

    Yields:
        `(in_window, in_box)` pairs — the window-filtered fixes (used by
        track mode, which draws the clipped line) and the
        window-and-bbox-filtered fixes (used by point mode).
    """
    south, north, west, east = bbox
    start, end = window
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
        yield in_window, in_box


def _empty_typed_fc(columns: dict[str, str]) -> FeatureCollection:
    """Return a zero-row `FeatureCollection` with a typed-but-empty schema.

    Args:
        columns: The schema (column -> pandas dtype) to materialise with no
            rows.

    Returns:
        FeatureCollection: Zero rows, `columns` with their dtypes, an empty
            `geometry`, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame(
        {column: pd.Series([], dtype=dtype) for column, dtype in columns.items()}
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=EVENT_CRS), crs=EVENT_CRS
    )
    return FeatureCollection(gdf)


def _concat_typed(
    collections: list[FeatureCollection],
    empty: Callable[[], FeatureCollection],
) -> FeatureCollection:
    """Row-union non-empty collections, falling back to an empty schema.

    Args:
        collections: Per-unit FeatureCollections sharing one schema + CRS.
        empty: Factory for the schema-correct empty result, used when every
            input is empty (or the list is empty).

    Returns:
        FeatureCollection: The row-wise union, CRS `EPSG:4326`.
    """
    non_empty = [fc for fc in collections if len(fc)]
    if not non_empty:
        return empty()
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
    if "time" in prepared.columns:
        time_col = "time"
    elif "date" in prepared.columns:
        time_col = "date"
    else:
        raise KeyError(
            "storm frame has no 'time' or 'date' column; cannot place fixes in "
            f"time (columns: {list(prepared.columns)})."
        )
    # Parse as UTC first, then strip the tz to get naive timestamps. tropycal's
    # best-track `time` column is tz-naive UTC; parsing with `utc=True` makes
    # the value tz-aware regardless of the input, so `tz_localize(None)` works
    # on every supported pandas (on pandas < 3.0 `tz_localize(None)` raises on
    # already-naive input, so the naive-in/naive-out shortcut is not portable).
    prepared["_time"] = pd.to_datetime(
        prepared.get(time_col), errors="coerce", utc=True
    ).dt.tz_localize(None)
    prepared["_lat"] = pd.to_numeric(prepared.get("lat"), errors="coerce")
    prepared["_lon"] = pd.to_numeric(prepared.get("lon"), errors="coerce")
    return prepared


def _base_point_frame(fixes: pd.DataFrame, source: str) -> pd.DataFrame:
    """Build the untyped `POINT_COLUMNS` attribute frame from in-box fixes.

    The shared core of the point mappers (`_points_to_fc` and the NHC
    best-track `_btk_points_to_fc`): the eleven track-field columns every
    point collection carries, before geometry and the schema cast. The
    NHC mapper then appends its radii columns.

    Args:
        fixes: All in-window/in-box fixes, concatenated across storms (with
            the `_time` / `_lat` / `_lon` helper columns).
        source: The source label stamped on every row.

    Returns:
        pd.DataFrame: The untyped attribute frame (one row per fix).
    """
    vmax = pd.to_numeric(_column(fixes, "vmax"), errors="coerce")
    return pd.DataFrame(
        {
            "storm_id": _column(fixes, "id").to_numpy(),
            "name": _column(fixes, "name").to_numpy(),
            "time": fixes["_time"].to_numpy(),
            "lat": fixes["_lat"].to_numpy(),
            "lon": fixes["_lon"].to_numpy(),
            "vmax_kt": vmax.to_numpy(),
            "mslp_hpa": pd.to_numeric(
                _column(fixes, "mslp"), errors="coerce"
            ).to_numpy(),
            "storm_type": _column(fixes, "type").to_numpy(),
            "category": _categories(vmax).to_numpy(),
            "basin": _column(fixes, "wmo_basin").to_numpy(),
            "source": source,
        }
    )


def _points_to_fc(fixes: pd.DataFrame, source: str) -> FeatureCollection:
    """Build the point-mode FeatureCollection from concatenated in-box fixes.

    Vectorised: every in-window/in-box fix across all storms is typed and
    mapped in one pass — no per-fix Python loop — so multi-decade,
    many-thousand-fix queries stay fast. The fixes have already been filtered
    to finite lat/lon by the bbox mask (NaN positions compare False and are
    dropped), so the `Point` geometry is always valid.

    Args:
        fixes: All in-window/in-box fixes, concatenated across storms (a
            `to_dataframe(attrs_as_columns=True)`-shaped frame with the
            `_time` / `_lat` / `_lon` helper columns).
        source: The requested data source, stamped on every row.

    Returns:
        FeatureCollection: Point-mode collection, CRS `EPSG:4326`.
    """
    frame = _base_point_frame(fixes, source)
    geoms = gpd.points_from_xy(frame["lon"], frame["lat"])
    return _frame_to_typed_fc(frame, geoms, POINT_COLUMNS)


def _btk_points_to_fc(fixes: pd.DataFrame, source: str) -> FeatureCollection:
    """Build the NHC best-track point FeatureCollection (track fields + radii).

    Extends `_points_to_fc` with the twelve `r34_ne` … `r64_nw` wind-radii
    columns read from the folded best-track fixes (absent thresholds stay
    `NaN`), and casts to the wider `BTK_POINT_COLUMNS` schema.

    Args:
        fixes: All in-window/in-box fixes, concatenated across storms, each
            carrying the `RADII_COLUMNS` as well as the track fields.
        source: The source label stamped on every row (`"nhc"`).

    Returns:
        FeatureCollection: Point-mode collection with radii, CRS `EPSG:4326`.
    """
    frame = _base_point_frame(fixes, source)
    for column in RADII_COLUMNS:
        frame[column] = pd.to_numeric(
            _column(fixes, column), errors="coerce"
        ).to_numpy()
    geoms = gpd.points_from_xy(frame["lon"], frame["lat"])
    return _frame_to_typed_fc(frame, geoms, BTK_POINT_COLUMNS)


def _categories(vmax: pd.Series) -> pd.Series:
    """Vectorised Saffir-Simpson category for a wind Series (knots).

    Same bins as `saffir_simpson_category` (single source of truth via
    `_SSHWS_UPPER_KT`); missing winds map to `0`.

    Args:
        vmax: Maximum-sustained-wind values in knots.

    Returns:
        An `int64` Series of categories in `0..5`.
    """
    bins = [float("-inf"), *_SSHWS_UPPER_KT, float("inf")]
    labels = list(range(len(_SSHWS_UPPER_KT) + 1))
    cats = pd.cut(vmax, bins=bins, labels=labels, right=True)
    return cats.astype("float64").fillna(0).astype("int64")


def _column(fixes: pd.DataFrame, name: str) -> pd.Series:
    """Return a frame column, or an all-null Series when the column is absent.

    Args:
        fixes: The (concatenated) fix frame.
        name: Column name to read.

    Returns:
        The column as a default-indexed Series, or an all-`None` Series of the
        same length when `name` is missing.
    """
    if name in fixes.columns:
        return fixes[name].reset_index(drop=True)
    return pd.Series([None] * len(fixes))


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
    coords = list(
        zip(positioned["_lon"].astype(float), positioned["_lat"].astype(float))
    )
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
        rows: Attribute rows keyed by `columns` (used by track mode, whose
            row count is the storm count — small).
        geoms: One shapely geometry per row (parallel to `rows`).
        columns: The mode schema (column -> pandas dtype).

    Returns:
        FeatureCollection: Typed collection, CRS `EPSG:4326`.
    """
    frame = pd.DataFrame(rows, columns=list(columns))
    return _frame_to_typed_fc(frame, geoms, columns)


def _frame_to_typed_fc(
    frame: pd.DataFrame,
    geoms: object,
    columns: dict[str, str],
) -> FeatureCollection:
    """Cast a frame to the mode schema and wrap it as a `FeatureCollection`.

    Args:
        frame: A DataFrame whose columns match `columns` (untyped).
        geoms: A geometry sequence (one per row), e.g. a
            `geopandas.array.GeometryArray` or list of shapely geometries.
        columns: The mode schema (column -> pandas dtype).

    Returns:
        FeatureCollection: Typed collection, CRS `EPSG:4326`.
    """
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
