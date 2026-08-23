"""Stateless helpers for the EEA (`eea_aq`) backend.

Three concerns the backend needs but that carry no instance state:

* `countries_in_bbox` — the airbase download service queries by country
  (ISO2), not by bounding box, so a request bbox is resolved to the EEA
  reporting countries whose own bounding box intersects it. The bundled
  `EEA_COUNTRY_BBOXES` table keeps this a pure-`tabular` lookup (no
  geometry dependency, no pyramids hand-off).
* `datasets_for_years` — the EEA splits its archive into three
  named datasets by reporting era (`Historical` 2002–2012, `Verified`
  2013–2022, `Unverified` 2023+); a requested year range is mapped to
  the dataset(s) that span it.
* `shape_frame` — the downloaded Parquet has no coordinates and labels
  pollutants by numeric code; this reshapes one raw Parquet frame into
  the backend's long-format schema.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
from loguru import logger

#: Approximate WGS84 bounding boxes `(lon_min, lat_min, lon_max, lat_max)`
#: for the EEA reporting countries (EU-27 + EFTA + Western Balkans + Turkey
#: + UK + reporting microstates). Mainland boxes only (overseas territories
#: and remote islands are omitted). Every code here is in airbase's served
#: `client.countries` set (e.g. `LI`/Liechtenstein is omitted — airbase does
#: not serve it); the backend additionally intersects the derived set with
#: the live `client.countries` at request time. Used to resolve a request
#: bbox to the countries whose data to download; pass an explicit `country=`
#: to skip this heuristic.
EEA_COUNTRY_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "AD": (1.41, 42.42, 1.79, 42.66),
    "AL": (19.29, 39.62, 21.06, 42.66),
    "AT": (9.53, 46.37, 17.16, 49.02),
    "BA": (15.72, 42.55, 19.62, 45.28),
    "BE": (2.51, 49.50, 6.41, 51.51),
    "BG": (22.34, 41.23, 28.61, 44.22),
    "CH": (5.96, 45.82, 10.49, 47.81),
    "CY": (32.26, 34.57, 34.60, 35.71),
    "CZ": (12.09, 48.55, 18.86, 51.06),
    "DE": (5.87, 47.27, 15.04, 55.06),
    "DK": (8.07, 54.56, 15.16, 57.75),
    "EE": (23.34, 57.52, 28.21, 59.68),
    "ES": (-9.39, 35.95, 3.32, 43.79),
    "FI": (20.55, 59.81, 31.59, 70.09),
    "FR": (-5.14, 41.33, 9.56, 51.09),
    "GB": (-8.65, 49.91, 1.77, 60.85),
    "GR": (19.37, 34.80, 28.25, 41.75),
    "HR": (13.50, 42.40, 19.43, 46.55),
    "HU": (16.11, 45.74, 22.90, 48.59),
    "IE": (-10.48, 51.42, -6.01, 55.39),
    "IS": (-24.55, 63.30, -13.50, 66.57),
    "IT": (6.63, 36.62, 18.52, 47.10),
    "LT": (20.94, 53.90, 26.84, 56.45),
    "LU": (5.73, 49.44, 6.53, 50.19),
    "LV": (20.97, 55.67, 28.24, 58.09),
    "ME": (18.43, 41.85, 20.36, 43.56),
    "MK": (20.45, 40.85, 23.03, 42.37),
    "MT": (14.18, 35.78, 14.58, 36.10),
    "NL": (3.31, 50.75, 7.23, 53.68),
    "NO": (4.65, 57.98, 31.29, 71.19),
    "PL": (14.12, 49.00, 24.15, 54.84),
    "PT": (-9.52, 36.96, -6.19, 42.15),
    "RO": (20.26, 43.62, 29.71, 48.27),
    "RS": (18.83, 42.23, 23.01, 46.19),
    "SE": (11.03, 55.34, 24.17, 69.06),
    "SI": (13.38, 45.42, 16.61, 46.88),
    "SK": (16.83, 47.73, 22.57, 49.61),
    "TR": (25.66, 35.82, 44.82, 42.11),
    "XK": (20.01, 41.86, 21.79, 43.27),
}

#: EEA dataset name -> inclusive `(first_year, last_year)` reporting era.
#: `Verified` is left open-ended (not capped at 2022) on purpose: the EEA
#: promotes a year from the near-real-time `Unverified` (E2a/UTD) stream into
#: `Verified` (E1a) once validated (~September of the following year), and the
#: UTD stream keeps only a rolling window. So any year from 2023 on is queried
#: against *both* `Verified` and `Unverified` and the rows de-duplicated,
#: rather than routed to a single era by a hard-coded cutoff that goes stale
#: every year. Years 2013–2022 still resolve to `Verified` alone (the
#: `Unverified` era starts in 2023 and does not intersect them).
EEA_DATASET_YEARS: dict[str, tuple[int, int]] = {
    "Historical": (2002, 2012),
    "Verified": (2013, 9999),
    "Unverified": (2023, 9999),
}

#: Verified <-> Unverified adjacency, used only for the empty-primary-era
#: fallback (`adjacent_eras`). The two live eras hold the same measurements at
#: different validation stages: the EEA promotes a year from `Unverified`
#: (E2a/UTD) into `Verified` (E1a) once validated, so a boundary year can be
#: missing from one while still present in the other. `Historical` has no
#: adjacency — it is a frozen archive with no live counterpart.
_ADJACENT_ERAS: dict[str, str] = {
    "Verified": "Unverified",
    "Unverified": "Verified",
}

#: Promotion-lag margin (years) by which a neighbour era's declared span is
#: widened when testing the empty-primary-era fallback. The EEA promotes a year
#: from `Unverified` into `Verified` ~September of the following year, so the
#: boundary year immediately below `Unverified`'s declared start can still be
#: sitting in the live `Unverified` stream; a one-year margin lets the fallback
#: reach it without falling back for genuinely out-of-range years (e.g. 2015),
#: which the neighbour era cannot hold and would only bulk-download in vain.
_PROMOTION_LAG_YEARS: int = 1

#: Long-format schema (column -> dtype) the backend returns, even for an
#: empty result, so callers always get the same shape.
SCHEMA: dict[str, str] = {
    "station_id": "object",
    "country": "object",
    "parameter": "object",
    "datetime_utc": "datetime64[ns, UTC]",
    "value": "float64",
    "units": "object",
    "agg_type": "object",
    "validity": "Int64",
    "verification": "Int64",
    "dataset": "object",
    "provider": "object",
}

#: `Value` literals the EEA writes to mean "no reading" rather than a real
#: concentration. The legacy Historical export fills an invalid row's `Value`
#: with `-999`; a negatively-flagged row is masked regardless of its value, so
#: this set is consulted only to catch a sentinel on a row whose `Validity`
#: flag is null (where the flag cannot betray it). Kept deliberately small — a
#: real reading must never be clipped — so it lists only the documented `-999`.
_NODATA_SENTINELS: frozenset[float] = frozenset({-999.0})


def countries_in_bbox(
    lat_lim: tuple[float, float],
    lon_lim: tuple[float, float],
    table: dict[str, tuple[float, float, float, float]] | None = None,
) -> list[str]:
    """Return the EEA countries whose bounding box intersects the request.

    A pure-`tabular` bbox-overlap test against `EEA_COUNTRY_BBOXES` (no
    geometry dependency). Two boxes intersect when they overlap on both
    axes.

    Args:
        lat_lim: `(lat_min, lat_max)` of the request bbox, in degrees.
        lon_lim: `(lon_min, lon_max)` of the request bbox, in degrees.
        table: Country -> `(lon_min, lat_min, lon_max, lat_max)` lookup.
            Defaults to `EEA_COUNTRY_BBOXES`.

    Returns:
        list[str]: The intersecting country ISO2 codes, sorted.

    Examples:
        - A box over the Low Countries picks its neighbours:
            ```python
            >>> from earthlens.eea_aq._helpers import countries_in_bbox
            >>> countries_in_bbox((50.8, 52.0), (4.0, 6.0))
            ['BE', 'DE', 'FR', 'NL']

            ```
    """
    table = table if table is not None else EEA_COUNTRY_BBOXES
    lat_min, lat_max = sorted(lat_lim)
    lon_min, lon_max = sorted(lon_lim)
    hits = [
        iso2
        for iso2, (clon_min, clat_min, clon_max, clat_max) in table.items()
        if clon_min <= lon_max
        and clon_max >= lon_min
        and clat_min <= lat_max
        and clat_max >= lat_min
    ]
    return sorted(hits)


def datasets_for_years(start_year: int, end_year: int) -> list[str]:
    """Return the EEA datasets spanning the inclusive `[start, end]` years.

    Maps a requested year range to the named reporting-era datasets
    (`EEA_DATASET_YEARS`) whose own year range intersects it, in
    chronological order.

    Args:
        start_year: First calendar year of the request (inclusive).
        end_year: Last calendar year of the request (inclusive).

    Returns:
        list[str]: The dataset names to download, chronologically ordered.

    Examples:
        - A range straddling the Verified/Unverified boundary needs both:
            ```python
            >>> from earthlens.eea_aq._helpers import datasets_for_years
            >>> datasets_for_years(2021, 2024)
            ['Verified', 'Unverified']
            >>> datasets_for_years(2010, 2011)
            ['Historical']

            ```
    """
    lo_hi = sorted((start_year, end_year))
    start_year, end_year = lo_hi
    return [
        name
        for name, (first, last) in EEA_DATASET_YEARS.items()
        if start_year <= last and end_year >= first
    ]


def adjacent_eras(datasets: list[str], start_year: int, end_year: int) -> list[str]:
    """Return the live era(s) adjacent to `datasets` that could hold the request.

    The fallback target when a primary sweep returns zero files: a live era
    (`Verified` / `Unverified`) paired with one already swept, not itself in
    `datasets`, and whose year span can plausibly cover `[start_year, end_year]`.
    A year straddling the promotion frontier can be missing from its primary era
    yet still present in the neighbour — a not-yet-promoted year sits in
    `Unverified` before it lands in `Verified` — so retrying the neighbour
    recovers it. The neighbour's declared span is widened by
    `_PROMOTION_LAG_YEARS` so that boundary year is reachable; a request whose
    years fall outside even that widened span is not returned, because the
    neighbour cannot hold it and would only be bulk-downloaded in vain.

    Returns `[]` when there is nothing worth trying: a recent-year request
    already spans both live eras, a `Historical`-only request has no live
    neighbour, and an out-of-range year (e.g. 2015 against `Unverified` 2023+)
    is filtered out.

    Args:
        datasets: The eras already swept, as returned by `datasets_for_years`.
        start_year: First calendar year of the request (inclusive).
        end_year: Last calendar year of the request (inclusive).

    Returns:
        list[str]: The adjacent live era(s) worth retrying, order-stable and
            de-duplicated.

    Examples:
        - A `Verified`-only request at the promotion boundary falls back, an
          out-of-range one does not, and a dual-era request has nothing to add:
            ```python
            >>> from earthlens.eea_aq._helpers import adjacent_eras
            >>> adjacent_eras(["Verified"], 2022, 2022)
            ['Unverified']
            >>> adjacent_eras(["Verified"], 2015, 2015)
            []
            >>> adjacent_eras(["Verified", "Unverified"], 2024, 2024)
            []

            ```
    """
    already = set(datasets)
    lo, hi = sorted((start_year, end_year))
    out: list[str] = []
    for name in datasets:
        neighbour = _ADJACENT_ERAS.get(name)
        if neighbour is None or neighbour in already or neighbour in out:
            continue
        first, last = EEA_DATASET_YEARS[neighbour]
        if lo <= last and hi >= first - _PROMOTION_LAG_YEARS:
            out.append(neighbour)
    return out


def shape_frame(
    raw: pd.DataFrame, dataset: str, code_to_name: dict[int, str]
) -> pd.DataFrame:
    """Reshape one raw EEA Parquet frame into the backend's long schema.

    The Parquet has no coordinates and labels each row's pollutant by a
    numeric EEA code; this maps codes back to names, parses the country
    from the `Samplingpoint` prefix (`"MT/SPO-..."` -> `"MT"`), coerces
    the string `Value` to float, and localises the naive `Start` timestamp
    to UTC. Rows whose numeric code is not in `code_to_name` are dropped
    (a pollutant the request did not ask for).

    A reading the EEA does not vouch for has its `value` masked to `NaN` so
    a no-data sentinel never masquerades as a measured concentration: a row
    with a negative `Validity` flag (`-1` invalid, `-99` maintenance) is
    masked whatever its `Value` (catching both the `-999` sentinel and the
    plain `0.0` some invalid rows carry), and a row with a null flag is
    masked when its `Value` is a known sentinel. The flag itself is kept in
    `validity`; valid rows keep their published value, small near-zero
    negatives included.

    Args:
        raw: One Parquet file read with `pandas.read_parquet`.
        dataset: The dataset era this frame came from (`"Verified"`),
            recorded in the `dataset` column.
        code_to_name: EEA numeric code -> earthlens pollutant name map.

    Returns:
        pd.DataFrame: The frame in the `SCHEMA` columns / dtypes.

    Examples:
        - A valid reading is kept while an invalid `-999` sentinel is masked to
          `NaN`, its `-1` flag preserved in `validity`:
            ```python
            >>> import pandas as pd
            >>> from earthlens.eea_aq._helpers import shape_frame
            >>> raw = pd.DataFrame(
            ...     {
            ...         "Samplingpoint": ["MT/SPO-1", "MT/SPO-1"],
            ...         "Pollutant": [6001, 6001],
            ...         "Start": pd.to_datetime(["2011-06-01", "2011-06-01"]),
            ...         "Value": ["14.6", "-999"],
            ...         "Unit": ["ug.m-3", "ug.m-3"],
            ...         "AggType": ["hour", "hour"],
            ...         "Validity": [1, -1],
            ...         "Verification": [3, 3],
            ...     }
            ... )
            >>> out = shape_frame(raw, "Historical", {6001: "pm25"})
            >>> out["value"].tolist()
            [14.6, nan]
            >>> out["validity"].tolist()
            [1, -1]
            >>> out["parameter"].tolist()
            ['pm25', 'pm25']

            ```
    """
    if raw.empty:
        return empty_frame()
    keep = raw[raw["Pollutant"].isin(code_to_name)].copy()
    if keep.empty:
        # Non-empty Parquet whose codes miss every requested pollutant is
        # usually an upstream `Pollutant`-column type/vocabulary drift (the
        # catalog codes are integers); surface a distinct diagnostic rather
        # than a silent empty frame so it is not mistaken for "no data".
        observed = sorted(set(raw["Pollutant"].tolist()))[:10]
        logger.warning(
            f"EEA reshape: {len(raw)} Parquet row(s) but none matched the "
            f"requested pollutant code(s) {sorted(code_to_name)}; observed "
            f"Pollutant value(s) {observed}. Possible airbase schema drift."
        )
        return empty_frame()
    out = pd.DataFrame(index=keep.index)
    out["station_id"] = keep["Samplingpoint"].astype("object")
    out["country"] = keep["Samplingpoint"].str.split("/").str[0]
    out["parameter"] = keep["Pollutant"].map(code_to_name)
    # The EEA download service publishes the observation period in UTC, so the
    # naive `Start` is localised (not converted) to UTC here; the date-window
    # filter in the backend is therefore correct at day boundaries.
    out["datetime_utc"] = pd.to_datetime(keep["Start"], utc=True)
    out["value"] = pd.to_numeric(keep["Value"], errors="coerce")
    out["units"] = keep["Unit"]
    out["agg_type"] = keep["AggType"]
    out["validity"] = keep["Validity"].astype("Int64")
    out["verification"] = keep["Verification"].astype("Int64")
    # Mask no-data readings to NaN so a caller's mean / percentile is not
    # silently skewed by them; a bare `value.dropna()` cannot help because the
    # sentinels are numbers, not nulls. EEA flags a reading it does not vouch
    # for with a negative `Validity` (-1 invalid, -99 maintenance) and fills its
    # `Value` with a sentinel (-999) or a plain 0.0 -- gate on the flag, not the
    # literal, so both are caught, while the flag itself is preserved in
    # `validity`. A null flag is trusted for neither verdict, so a null-flag row
    # is masked only when its value is a known sentinel, leaving a genuine
    # reading that merely lacks a flag untouched.
    invalid_flag = (out["validity"] < 0).fillna(False)
    unflagged_sentinel = out["validity"].isna() & out["value"].isin(_NODATA_SENTINELS)
    out.loc[invalid_flag | unflagged_sentinel, "value"] = pd.NA
    out["dataset"] = dataset
    out["provider"] = "EEA"
    return out.reset_index(drop=True).astype(SCHEMA)


def empty_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the exact long-format schema.

    Returns:
        pd.DataFrame: Zero rows, `SCHEMA` columns and dtypes.
    """
    return pd.DataFrame({column: [] for column in SCHEMA}).astype(SCHEMA)


def _patch_running_loop() -> None:
    """Allow airbase's `asyncio.run()` to nest under a running event loop.

    airbase's `download()` calls `asyncio.run()` internally, which raises
    `RuntimeError` when a loop is already running (e.g. inside a Jupyter
    kernel). In a plain script there is no running loop and this is a no-op;
    under a running loop it applies `nest_asyncio` (a declared dependency of
    the `[eea_aq]` extra) so the nested `run()` succeeds — keeping the
    download on the calling thread (airbase's SQLite session is thread-bound,
    so a worker thread is not an option).

    Raises:
        RuntimeError: When a loop is running but `nest_asyncio` is not
            installed; the message names the fix.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        import nest_asyncio
    except ImportError as exc:  # pragma: no cover - only without nest_asyncio
        raise RuntimeError(
            "eea_aq download is running inside an active event loop (e.g. a "
            "Jupyter kernel); install nest_asyncio (`pip install nest_asyncio`) "
            "so airbase's asyncio.run() can nest."
        ) from exc
    nest_asyncio.apply()


def download_request(request: Any, directory: str) -> None:
    """Run an airbase `request.download()`, tolerating a running event loop.

    Args:
        request: The `airbase.AirbaseRequest` (or a compatible stand-in).
        directory: Destination directory for the downloaded Parquet.
    """
    _patch_running_loop()
    request.download(dir=directory, skip_existing=True, raise_for_status=True)
