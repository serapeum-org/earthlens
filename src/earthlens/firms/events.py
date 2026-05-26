"""Map a FIRMS area-CSV response into a pyramids `FeatureCollection`.

This module is the only place in the FIRMS backend that touches a GIS
vector container, so per the pyramids policy it keeps all geometry/CRS
handling inside pyramids primitives: earthlens assembles the normalised
attribute rows, builds a `Point` geometry column from `longitude` /
`latitude`, and hands the whole thing to
:class:`pyramids.feature.collection.FeatureCollection` (a
`geopandas.GeoDataFrame` subclass) tagged `EPSG:4326`.

The canonical detection schema lives here as :data:`ATTRIBUTE_COLUMNS`
(attribute columns + dtypes) plus the `geometry` column. Both the
populated path (:func:`csv_to_fc`) and the empty path (:func:`empty_fc`)
produce a FeatureCollection with exactly these columns and dtypes, so a
downstream `to_file` never chokes on a schema mismatch between a hit and
a miss.

Two FIRMS data-shape wrinkles are absorbed here:

* **Confidence differs by sensor family** (`G4`). MODIS reports a
  numeric 0-100 `confidence`; VIIRS reports a categorical `l`/`n`/`h`
  token. The mapper keeps the raw value in `confidence` *and* derives a
  uniform `confidence_pct` float — VIIRS `l`/`n`/`h` map to 25/60/90,
  MODIS passes through. A single `brightness_k` column is filled from
  `brightness` (MODIS) or `bright_ti4` (VIIRS), whichever the sensor
  provides.
* **`acq_time` is an unpadded integer HHMM** (e.g. `5` = 00:05,
  `1325` = 13:25), so it is split into hours/minutes and added to
  `acq_date` rather than string-concatenated.

Columns are read defensively (`.get` / column-presence checks) so a
sensor missing a column degrades to `NaN`/`None` rather than raising.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from loguru import logger
from pyramids.feature.collection import FeatureCollection

#: WGS84 — the CRS every FIRMS detection FeatureCollection is tagged with.
DETECTION_CRS = "EPSG:4326"

#: VIIRS reports confidence as a categorical token (low/nominal/high);
#: these are the representative percentages the `confidence_pct` column
#: uses.
VIIRS_CONFIDENCE_PCT: dict[str, float] = {"l": 25.0, "n": 60.0, "h": 90.0}

#: LANDSAT reports confidence as low/medium/high tokens.
LANDSAT_CONFIDENCE_PCT: dict[str, float] = {"l": 25.0, "m": 60.0, "h": 90.0}

#: Sensor families whose `confidence` is a categorical token, with the
#: token → percent map each uses. Families absent here (MODIS, GOES)
#: report a numeric confidence that passes through `pandas.to_numeric`.
_CATEGORICAL_CONFIDENCE: dict[str, dict[str, float]] = {
    "VIIRS": VIIRS_CONFIDENCE_PCT,
    "LANDSAT": LANDSAT_CONFIDENCE_PCT,
}

#: The CSV column each family carries its brightness temperature in.
#: `None` means the family has no brightness column (LANDSAT), so
#: `brightness_k` degrades to `NaN`.
_BRIGHTNESS_SOURCE: dict[str, str | None] = {
    "MODIS": "brightness",
    "VIIRS": "bright_ti4",
    "GOES": "bright_ti4",
    "LANDSAT": None,
}

#: Families whose `confidence_pct` is a genuine 0-100 percent — MODIS is
#: numeric 0-100, VIIRS/LANDSAT map their tokens onto 0-100. GOES is
#: excluded: its confidence is a provider-defined value (~0-1), not a
#: percent, so a `min_confidence` threshold is meaningless on it and is
#: skipped (with a warning) rather than silently dropping every row.
PERCENT_CONFIDENCE_FAMILIES: frozenset[str] = frozenset({"MODIS", "VIIRS", "LANDSAT"})

#: Ordered attribute columns and their pandas dtypes. The `geometry`
#: column is added separately by :func:`csv_to_fc` / :func:`empty_fc`.
ATTRIBUTE_COLUMNS: dict[str, str] = {
    "latitude": "float64",
    "longitude": "float64",
    "acq_datetime": "datetime64[ns, UTC]",
    "sensor": "string",
    "satellite": "string",
    "confidence": "string",
    "confidence_pct": "float64",
    "brightness_k": "float64",
    "frp": "float64",
    "daynight": "string",
}


def csv_to_fc(
    df: pd.DataFrame,
    sensor: str,
    family: str,
    min_confidence: float | None = None,
    day_night: str | None = None,
) -> FeatureCollection:
    """Normalise one sensor's FIRMS CSV frame into a `FeatureCollection`.

    One row per detection, columns per :data:`ATTRIBUTE_COLUMNS` plus a
    `geometry` column of `Point(longitude, latitude)`. The MODIS/VIIRS
    confidence and brightness schemas are unified (`G4`), `acq_date` +
    integer-HHMM `acq_time` are combined into a tz-aware UTC
    `acq_datetime`, and the optional `min_confidence` / `day_night`
    filters are applied client-side (FIRMS offers no server-side
    equivalent). An empty input frame returns an empty FeatureCollection
    with the same columns/dtypes (see :func:`empty_fc`).

    Args:
        df: The decoded FIRMS area CSV for one sensor/chunk.
        sensor: The FIRMS sensor code; recorded in the `sensor` column.
        family: `"MODIS"` or `"VIIRS"` — selects the confidence and
            brightness source columns.
        min_confidence: Optional 0-100 lower bound on the normalised
            `confidence_pct`; rows below it are dropped. Applied only to
            families whose confidence is a true 0-100 percent
            (MODIS / VIIRS / LANDSAT); for GOES (a provider-scale numeric
            confidence) the filter is skipped with a warning rather than
            silently dropping every row. `None` keeps all.
        day_night: Optional `"D"` / `"N"` filter on the `daynight`
            column. `None` keeps both.

    Returns:
        FeatureCollection: One feature per surviving detection, CRS
            `EPSG:4326`. Empty (schema-only) when the input is empty or
            the filters drop everything.

    Examples:
        - Map a one-row VIIRS frame; the `l` token becomes 25 %:
            ```python
            >>> import pandas as pd
            >>> from earthlens.firms.events import csv_to_fc
            >>> df = pd.DataFrame(
            ...     {
            ...         "latitude": [34.0],
            ...         "longitude": [-118.0],
            ...         "acq_date": ["2024-08-01"],
            ...         "acq_time": [1325],
            ...         "satellite": ["N"],
            ...         "confidence": ["l"],
            ...         "bright_ti4": [320.0],
            ...         "frp": [12.5],
            ...         "daynight": ["D"],
            ...     }
            ... )
            >>> fc = csv_to_fc(df, "VIIRS_SNPP_NRT", "VIIRS")
            >>> float(fc["confidence_pct"].iloc[0])
            25.0
            >>> fc["acq_datetime"].iloc[0].strftime("%Y-%m-%d %H:%M")
            '2024-08-01 13:25'
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    if df is None or df.empty:
        return empty_fc()

    frame = pd.DataFrame(index=df.index)
    frame["latitude"] = pd.to_numeric(df.get("latitude"), errors="coerce")
    frame["longitude"] = pd.to_numeric(df.get("longitude"), errors="coerce")
    frame["acq_datetime"] = _acq_datetime(df)
    frame["sensor"] = sensor
    frame["satellite"] = _as_string(df.get("satellite"))
    raw_confidence = df.get("confidence")
    frame["confidence"] = _as_string(raw_confidence)
    frame["confidence_pct"] = _confidence_pct(raw_confidence, family)
    frame["brightness_k"] = _brightness(df, family)
    frame["frp"] = _numeric(df, "frp")
    frame["daynight"] = _as_string(df.get("daynight"))

    if min_confidence is not None:
        if family in PERCENT_CONFIDENCE_FAMILIES:
            frame = frame[frame["confidence_pct"] >= min_confidence]
        else:
            logger.warning(
                f"min_confidence={min_confidence} not applied to {sensor}: "
                f"{family} reports a provider-scale (non 0-100) confidence, so "
                "thresholding it would silently drop every detection. Rows kept "
                "unfiltered — filter on the raw `confidence` column yourself if "
                "needed."
            )
    if day_night is not None:
        frame = frame[frame["daynight"] == day_night]

    if frame.empty:
        return empty_fc()

    frame = frame.reset_index(drop=True)
    for column, dtype in ATTRIBUTE_COLUMNS.items():
        if column == "acq_datetime":
            frame[column] = frame[column].astype(dtype)
        elif dtype == "string":
            frame[column] = frame[column].astype("string")
    geometry = gpd.points_from_xy(frame["longitude"], frame["latitude"])
    gdf = gpd.GeoDataFrame(
        frame[list(ATTRIBUTE_COLUMNS)],
        geometry=gpd.GeoSeries(geometry, crs=DETECTION_CRS),
        crs=DETECTION_CRS,
    )
    return FeatureCollection(gdf)


def concat(collections: list[FeatureCollection]) -> FeatureCollection:
    """Concatenate per-chunk collections into one, schema-stable.

    Args:
        collections: The per-`(sensor, chunk)` collections to merge.

    Returns:
        FeatureCollection: Their row-wise union, CRS `EPSG:4326`, or a
            schema-only empty collection when every input was empty.

    Examples:
        - Concatenating only empty collections returns an empty one:
            ```python
            >>> from earthlens.firms.events import concat, empty_fc
            >>> len(concat([empty_fc(), empty_fc()]))
            0

            ```
    """
    non_empty = [fc for fc in collections if len(fc)]
    if not non_empty:
        return empty_fc()
    merged = pd.concat(non_empty, ignore_index=True)
    gdf = gpd.GeoDataFrame(merged, geometry="geometry", crs=DETECTION_CRS)
    return FeatureCollection(gdf)


def empty_fc() -> FeatureCollection:
    """Return an empty `FeatureCollection` with the canonical schema.

    Used for an empty CSV, an out-of-coverage window, or a request whose
    filters dropped every row, so callers always get the same columns
    and dtypes back regardless of hit count.

    Returns:
        FeatureCollection: Zero rows, the :data:`ATTRIBUTE_COLUMNS`
            columns with their declared dtypes, an empty `geometry`
            column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.firms.events import empty_fc, ATTRIBUTE_COLUMNS
            >>> fc = empty_fc()
            >>> len(fc)
            0
            >>> set(ATTRIBUTE_COLUMNS).issubset(fc.columns)
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    frame = pd.DataFrame(
        {
            column: pd.Series([], dtype=dtype)
            for column, dtype in ATTRIBUTE_COLUMNS.items()
        }
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=DETECTION_CRS), crs=DETECTION_CRS
    )
    return FeatureCollection(gdf)


def _acq_datetime(df: pd.DataFrame) -> pd.Series:
    """Combine `acq_date` + integer-HHMM `acq_time` into UTC datetimes.

    FIRMS sends `acq_time` as an unpadded integer (e.g. `5` = 00:05,
    `1325` = 13:25), so it is split into hours/minutes rather than
    string-concatenated. A missing or unparseable value degrades to
    `NaT`.

    Args:
        df: One sensor's CSV frame.

    Returns:
        pd.Series: tz-aware UTC datetimes aligned to `df.index`.
    """
    dates = pd.to_datetime(df.get("acq_date"), errors="coerce", utc=True)
    times = pd.to_numeric(df.get("acq_time"), errors="coerce").fillna(0).astype(int)
    minutes = (times // 100) * 60 + (times % 100)
    return dates + pd.to_timedelta(minutes, unit="m")


def _confidence_pct(raw: pd.Series | None, family: str) -> pd.Series:
    """Derive a uniform 0-100 confidence from the raw column.

    Categorical families map their tokens via
    :data:`_CATEGORICAL_CONFIDENCE` (VIIRS `l`/`n`/`h` → 25/60/90,
    LANDSAT `l`/`m`/`h` → 25/60/90); numeric families (MODIS, GOES) pass
    through coerced to float. An unknown token / value becomes `NaN`.
    GOES reports a provider-defined numeric confidence (not a 0-100
    percent); it passes through unscaled.

    Args:
        raw: The raw `confidence` column, or `None` when absent.
        family: One of `"MODIS"`, `"VIIRS"`, `"GOES"`, `"LANDSAT"`.

    Returns:
        pd.Series: The normalised confidence as floats.
    """
    if raw is None:
        return np.nan
    mapping = _CATEGORICAL_CONFIDENCE.get(family)
    if mapping is not None:
        tokens = raw.astype("string").str.strip().str.lower()
        return tokens.map(mapping).astype("float64")
    return pd.to_numeric(raw, errors="coerce")


def _brightness(df: pd.DataFrame, family: str) -> pd.Series:
    """Fill `brightness_k` from the sensor-family-specific column.

    MODIS uses `brightness`; VIIRS and GOES use `bright_ti4`; LANDSAT
    carries no brightness column, so it degrades to all-`NaN`. A family
    whose expected column is absent from the frame also degrades to
    `NaN`.

    Args:
        df: One sensor's CSV frame.
        family: One of `"MODIS"`, `"VIIRS"`, `"GOES"`, `"LANDSAT"`.

    Returns:
        pd.Series: The brightness temperature in kelvin.
    """
    source = _BRIGHTNESS_SOURCE.get(family, "brightness")
    column = df.get(source) if source is not None else None
    if column is None:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(column, errors="coerce")


def _numeric(df: pd.DataFrame, name: str) -> pd.Series:
    """Coerce one CSV column to float, degrading an absent column to `NaN`.

    LANDSAT, for example, carries no `frp` column; rather than letting
    `pandas.to_numeric(None)` raise, an absent column yields an all-`NaN`
    series aligned to `df`.

    Args:
        df: One sensor's CSV frame.
        name: The column name to coerce.

    Returns:
        pd.Series: The column as floats, or all-`NaN` when absent.
    """
    column = df.get(name)
    if column is None:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(column, errors="coerce")


def _as_string(column: pd.Series | None) -> pd.Series | str:
    """Return a column as nullable strings, or a null literal when absent.

    Args:
        column: A CSV column, or `None` when the sensor omitted it.

    Returns:
        The column cast to the pandas `string` dtype, or `pd.NA` when
        the source column was absent (broadcast on assignment).
    """
    if column is None:
        return pd.NA
    return column.astype("string")
