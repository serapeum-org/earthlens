"""Attach affected-region geometry to HANZE flood events.

This module is the only place in the HANZE backend that touches a GIS vector
container, so per the pyramids policy it keeps all geometry / CRS handling inside
pyramids primitives. Each HANZE event names the NUTS-3 regions it affected as a
semicolon-separated code list in the `Regions affected (NUTS 3)` column; this
module splits that list, counts how many of the (already-filtered) events touch
each region, joins those codes to the NUTS-3 boundary polygons on the shapefile's
`Code` field, reprojects the result from the shapefile's stored ETRS89-LAEA CRS
(`EPSG:3035`) to WGS84, and returns a pyramids
:class:`~pyramids.feature.collection.FeatureCollection` of one polygon per
affected region.

The output schema is the same on the populated path (:func:`join_events_to_regions`)
and the empty path (:func:`empty_region_fc`) — `nuts3_code`, `region_name`,
`n_events`, and `geometry` — so a downstream `to_file` never chokes on a schema
mismatch between a hit and a miss.

Because HANZE's impact figures (fatalities, losses) are per *event* national /
multi-region totals rather than per-region values, they are deliberately **not**
summed onto the regions — doing so would double-count. The one honest per-region
metric is `n_events`, the number of the filtered events that affected each
region, which is what a choropleth map should show.
"""

from __future__ import annotations

from collections import Counter

import geopandas as gpd
import pandas as pd
from pyramids.feature.collection import FeatureCollection

#: WGS84 — the CRS every returned region FeatureCollection is tagged with, for a
#: degree bbox filter and parity with the other vector backends.
OUTPUT_CRS = "EPSG:4326"

#: Ordered columns of the region FeatureCollection (plus the `geometry` column,
#: added separately). `n_events` is the count of filtered events affecting the
#: region; the impact totals are event-level and are not attributed per region.
REGION_COLUMNS: dict[str, str] = {
    "nuts3_code": "string",
    "region_name": "string",
    "n_events": "int64",
}


def split_nuts3(value: object) -> list[str]:
    """Split one `Regions affected (NUTS 3)` cell into NUTS-3 codes.

    The cell is a semicolon-separated list (`"AL011;AL012;AL013"`); surrounding
    whitespace and empty segments are dropped. A missing / non-string cell
    yields an empty list.

    Args:
        value: One cell of the `Regions affected (NUTS 3)` column.

    Returns:
        list[str]: The NUTS-3 codes, in order, with blanks removed.

    Examples:
        - A semicolon list splits into its codes; a blank cell yields nothing:
            ```python
            >>> from earthlens.hanze.geometry import split_nuts3
            >>> split_nuts3("AL011; AL012 ;;AL013")
            ['AL011', 'AL012', 'AL013']
            >>> split_nuts3(None)
            []

            ```
    """
    if not isinstance(value, str):
        return []
    return [code.strip() for code in value.split(";") if code.strip()]


def event_region_counts(events: pd.DataFrame, regions_column: str) -> Counter[str]:
    """Count how many events affect each NUTS-3 region.

    Args:
        events: The filtered events table.
        regions_column: The column holding the semicolon-separated NUTS-3 code
            list (`"Regions affected (NUTS 3)"`).

    Returns:
        Counter[str]: NUTS-3 code -> number of events referencing it. Each code
            is counted at most once per event, even if it appears twice in that
            event's list.
    """
    counts: Counter[str] = Counter()
    if regions_column not in events.columns:
        return counts
    for cell in events[regions_column]:
        # Upper-case the codes so the join stays in lockstep with the tabular
        # filter (`_row_matches_codes` / `_bbox_region_codes`), which compare
        # upper-cased sets — the two output kinds must not diverge on case skew.
        counts.update({code.upper() for code in split_nuts3(cell)})
    return counts


def empty_region_fc() -> FeatureCollection:
    """Return an empty region `FeatureCollection` with the canonical schema.

    Used when the filtered events reference no region present in the boundary
    file, so callers always get the same columns / dtypes back regardless of hit
    count.

    Returns:
        FeatureCollection: Zero rows, the :data:`REGION_COLUMNS` columns with
            their dtypes, an empty `geometry` column, CRS `EPSG:4326`.

    Examples:
        - The schema is present even with no rows:
            ```python
            >>> from earthlens.hanze.geometry import empty_region_fc, REGION_COLUMNS
            >>> fc = empty_region_fc()
            >>> len(fc)
            0
            >>> set(REGION_COLUMNS).issubset(fc.columns)
            True
            >>> fc.crs.to_epsg()
            4326

            ```
    """
    frame = pd.DataFrame(
        {column: pd.Series([], dtype=dtype) for column, dtype in REGION_COLUMNS.items()}
    )
    gdf = gpd.GeoDataFrame(
        frame, geometry=gpd.GeoSeries([], crs=OUTPUT_CRS), crs=OUTPUT_CRS
    )
    return FeatureCollection(gdf)


def join_events_to_regions(
    events: pd.DataFrame,
    regions: FeatureCollection,
    *,
    regions_column: str,
    join_field: str,
    name_field: str,
) -> FeatureCollection:
    """Join filtered events to their affected NUTS-3 region polygons.

    Splits each event's `Regions affected (NUTS 3)` list, counts the events per
    region, selects the boundary polygons whose `join_field` is among the
    affected codes, reprojects them to WGS84, and returns one feature per
    affected region carrying its code, name and event count.

    Args:
        events: The filtered events table.
        regions: The NUTS-3 boundary polygons, in the shapefile's stored CRS
            (`EPSG:3035`), carrying `join_field` and `name_field` attributes.
        regions_column: The events column holding the semicolon-separated NUTS-3
            code list (`"Regions affected (NUTS 3)"`).
        join_field: The boundary attribute holding the NUTS-3 code (`"Code"`).
        name_field: The boundary attribute holding the region name (`"Name"`).

    Returns:
        FeatureCollection: One polygon per affected region, columns
            `nuts3_code` / `region_name` / `n_events` / `geometry`, CRS
            `EPSG:4326`. Empty (schema-only) when no affected code is present in
            the boundary file.
    """
    counts = event_region_counts(events, regions_column)
    if not counts or join_field not in regions.columns:
        return empty_region_fc()

    # Compare upper-cased on both sides so the selection tolerates any case skew
    # between the events column and the boundary file's `Code` field, matching
    # the tabular path's normalisation.
    selected = regions[regions[join_field].astype(str).str.upper().isin(counts.keys())]
    if not len(selected):
        return empty_region_fc()

    # Reproject to WGS84 first; the shapefile is ETRS89-LAEA (EPSG:3035). Reset
    # the index so the geometry `GeoSeries` (built from a positional numpy array
    # below) aligns with `frame` row-for-row: `selected` keeps the boundary
    # file's original scattered indices, and geopandas aligns a `GeoSeries` to
    # the frame *by index*, so without this a region at a high original index
    # would be paired with a missing (null) geometry.
    reprojected = selected.to_crs(OUTPUT_CRS).reset_index(drop=True)
    frame = pd.DataFrame(
        {
            "nuts3_code": reprojected[join_field].astype("string"),
            "region_name": reprojected[name_field].astype("string")
            if name_field in reprojected.columns
            else pd.Series([pd.NA] * len(reprojected), dtype="string"),
            "n_events": [counts[str(code).upper()] for code in reprojected[join_field]],
        }
    ).astype(REGION_COLUMNS)
    gdf = gpd.GeoDataFrame(
        frame,
        geometry=gpd.GeoSeries(reprojected.geometry.to_numpy(), crs=OUTPUT_CRS),
        crs=OUTPUT_CRS,
    )
    return FeatureCollection(gdf)
