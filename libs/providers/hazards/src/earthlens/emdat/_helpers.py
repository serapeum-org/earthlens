"""Stateless helpers for the EM-DAT backend.

Everything here is a pure function over its arguments: resolving a file on a
Dataverse installation, unpacking a GDIS granule, turning a request into an OGR
attribute filter, and applying the hazard / country / year / bbox filters that
are common to both routes. The backend owns the transport and the state; this
module owns the shaping.
"""

from __future__ import annotations

import warnings
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from pyramids.feature.collection import FeatureCollection

    from earthlens.base import HttpClient
    from earthlens.emdat.catalog import Dataset


class UngeocodedRowsWarning(UserWarning):
    """A bounding box discarded rows the source never gave coordinates for.

    Raised rather than returned quietly because the drop is a property of the
    source, not of the request: the EM-DAT archive geocodes roughly one event
    in ten, so a bbox on it silently removes most of the table.
    """


#: CRS of every EM-DAT / GDIS coordinate: plain lon/lat degrees.
CRS: str = "EPSG:4326"

#: Dataverse's "latest published version" alias.
_LATEST = ":latest"


def dataverse_file_listing(
    http: HttpClient, base: str, doi: str
) -> list[dict[str, Any]]:
    """List the files in a Dataverse dataset's latest published version.

    Args:
        http: The client used for the request.
        base: Base URL of the Dataverse installation.
        doi: The dataset's persistent id (`"doi:10.14428/DVN/I0LTPH"`).

    Returns:
        list[dict[str, Any]]: One entry per file, as returned by the Dataverse
            native API.

    Raises:
        requests.HTTPError: If the Dataverse API returns a non-2xx status.
    """
    url = f"{base.rstrip('/')}/api/datasets/:persistentId/versions/{_LATEST}/files"
    payload = http.get_json(url, params={"persistentId": doi})
    return list(payload.get("data") or [])


def pick_dataverse_file(
    files: list[dict[str, Any]], dataset: Dataset
) -> tuple[int, str]:
    """Find the data file for `dataset` in a Dataverse file listing.

    The archive file name carries the release date of the version that produced
    it, so it changes every time the archive is re-cut. Matching by pattern
    keeps the catalog valid across versions; a pinned file id would silently
    resolve to a stale release, or 404.

    Args:
        files: The listing from :func:`dataverse_file_listing`.
        dataset: The catalog row whose `file_pattern` selects the file.

    Returns:
        tuple[int, str]: The Dataverse file id and its file name.

    Raises:
        ValueError: If no file (or more than one) matches the pattern.
    """
    matches = [
        (int(entry["dataFile"]["id"]), str(entry["dataFile"]["filename"]))
        for entry in files
        if dataset.matches_file(str(entry.get("dataFile", {}).get("filename", "")))
    ]
    if not matches:
        names = sorted(
            str(entry.get("dataFile", {}).get("filename", "")) for entry in files
        )
        raise ValueError(
            f"no file matching {dataset.file_pattern!r} in the latest version of "
            f"{dataset.doi}. Files present: {names}. The archive layout may have "
            "changed; update `file_pattern:` in the EM-DAT catalog."
        )
    if len(matches) > 1:
        raise ValueError(
            f"{dataset.file_pattern!r} matched {len(matches)} files in "
            f"{dataset.doi}: {[name for _, name in matches]}. Tighten "
            "`file_pattern:` in the EM-DAT catalog so it names exactly one."
        )
    return matches[0]


def dataverse_download_url(base: str, file_id: int) -> str:
    """Build the direct-download URL for one Dataverse file.

    Args:
        base: Base URL of the Dataverse installation.
        file_id: The numeric file id.

    Returns:
        str: The `/api/access/datafile/<id>` URL.
    """
    return f"{base.rstrip('/')}/api/access/datafile/{file_id}"


def extract_member(archive: Path, member: str, dest_dir: Path) -> Path:
    """Extract one named member from a zip archive.

    Args:
        archive: Path to the `.zip`.
        member: The member's name inside the archive.
        dest_dir: Directory to extract into.

    Returns:
        Path: The extracted file. Returned as-is when it already exists, so a
            repeat call does not rewrite a multi-gigabyte member.

    Raises:
        ValueError: If `member` is not in the archive.
    """
    target = dest_dir / Path(member).name
    if target.exists():
        return target
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if member not in names:
            raise ValueError(
                f"{member!r} is not in {archive.name}; members are {names}. The "
                "granule layout may have changed; update `member:` in the "
                "EM-DAT catalog."
            )
        bundle.extract(member, dest_dir)
    extracted = dest_dir / member
    if extracted != target:
        extracted.replace(target)
        # A member stored under a directory leaves that directory behind
        # once the file is moved out of it.
        for parent in extracted.parents:
            if parent == dest_dir or not parent.is_dir():
                break
            try:
                parent.rmdir()
            except OSError:
                break
    return target


def hazard_filter_sql(column: str, hazards: list[str]) -> str:
    """Build an OGR attribute filter matching any of `hazards`.

    The shipped GDIS data is not internally consistent — the GeoPackage spells
    one value `"extreme temperature "` with a trailing space while the same
    table on Earth Engine spells it without — so each canonical name is matched
    both bare and space-suffixed.

    `LIKE` rather than `=`: with no wildcards it is an equality test, but one
    that ignores ASCII case in both the SQLite and OGR SQL dialects. A re-issued
    file that capitalised the value differently would silently return nothing
    under `=`, while the in-memory path (which compares stripped and lowered)
    would still match — the two halves of the same filter disagreeing. `TRIM()`
    is avoided because the available SQL functions vary by driver.

    Args:
        column: The attribute column holding the disaster type.
        hazards: Canonical hazard names (already normalised).

    Returns:
        str: A `WHERE`-clause fragment.
    """
    terms = []
    for hazard in hazards:
        escaped = hazard.replace("'", "''").replace("%", r"\%").replace("_", r"\_")
        terms.append(f"{column} LIKE '{escaped}'")
        terms.append(f"{column} LIKE '{escaped} '")
    return " OR ".join(terms)


def country_filter_sql(column: str, country: str) -> str:
    """Build an OGR attribute filter matching one ISO3 code.

    GDIS stores upper-case ISO3 codes, but `LIKE` is used rather than `=` so a
    re-issued file that changed the casing still matches — the in-memory path
    compares case-insensitively, and the two must not disagree.

    Args:
        column: The attribute column holding the ISO3 code.
        country: The ISO3 code to keep.

    Returns:
        str: A `WHERE`-clause fragment.
    """
    escaped = country.strip().upper().replace("'", "''")
    return f"{column} LIKE '{escaped}'"


def combine_filters(*clauses: str | None) -> str | None:
    """Join the supplied `WHERE` fragments with `AND`.

    Args:
        *clauses: Fragments, any of which may be `None` when that filter was
            not requested.

    Returns:
        str | None: The combined clause, or `None` when nothing was requested.
    """
    present = [clause for clause in clauses if clause]
    if not present:
        return None
    return " AND ".join(f"({clause})" for clause in present)


def event_years(frame: pd.DataFrame, dataset: Dataset) -> pd.Series:
    """Return the event year for every row as a nullable integer series.

    Most distributions name a year column outright. The GDIS GeoPackage does
    not carry one at all, so its year is recovered from the 4-digit prefix of
    the disaster number (`"2009-0631"` -> `2009`).

    Args:
        frame: The rows to read.
        dataset: The catalog row describing where the year lives.

    Returns:
        pandas.Series: The per-row year, `Int64`-typed so missing values
            survive.
    """
    if dataset.year_column and dataset.year_column in frame.columns:
        source = frame[dataset.year_column]
    elif dataset.year_from_id_prefix and dataset.id_column in frame.columns:
        source = frame[dataset.id_column].astype("string").str.slice(0, 4)
    else:
        return pd.Series(pd.NA, index=frame.index, dtype="Int64")
    return pd.to_numeric(source, errors="coerce").astype("Int64")


def filter_frame(
    frame: pd.DataFrame,
    dataset: Dataset,
    *,
    hazards: list[str] | None = None,
    country: str | None = None,
    year_range: tuple[int | None, int | None] = (None, None),
    bbox: tuple[float, float, float, float] | None = None,
) -> pd.DataFrame:
    """Apply the hazard / country / year / bbox filters to a table.

    Every filter is optional and skipped when the table lacks the column it
    needs, so the same routine serves the EM-DAT archive and both GDIS
    distributions despite their different schemas.

    Args:
        frame: The rows to filter.
        dataset: The catalog row naming this distribution's columns.
        hazards: Canonical hazard names to keep. `None` keeps every type.
        country: ISO3 code to keep. `None` keeps every country.
        year_range: Inclusive `(first, last)` year bounds; either may be
            `None`.
        bbox: `(min_lon, min_lat, max_lon, max_lat)` to keep, applied only when
            the table carries coordinates.

    Returns:
        pandas.DataFrame: The surviving rows, with the original index reset.

    Warns:
        UngeocodedRowsWarning: When a `bbox` discards rows that carry no
            coordinates at all.
    """
    mask = pd.Series(True, index=frame.index)

    if hazards and dataset.type_column and dataset.type_column in frame.columns:
        wanted = {hazard.strip().lower() for hazard in hazards}
        types = frame[dataset.type_column].astype("string").str.strip().str.lower()
        mask &= types.isin(wanted).fillna(False)

    if country and dataset.iso_column and dataset.iso_column in frame.columns:
        codes = frame[dataset.iso_column].astype("string").str.strip().str.upper()
        mask &= (codes == country.strip().upper()).fillna(False)

    first, last = year_range
    if first is not None or last is not None:
        years = event_years(frame, dataset)
        if first is not None:
            mask &= (years >= first).fillna(False)
        if last is not None:
            mask &= (years <= last).fillna(False)

    lat_col, lon_col = dataset.latitude_column, dataset.longitude_column
    if bbox and lat_col and lon_col and {lat_col, lon_col} <= set(frame.columns):
        min_lon, min_lat, max_lon, max_lat = bbox
        lats = pd.to_numeric(frame[lat_col], errors="coerce")
        lons = pd.to_numeric(frame[lon_col], errors="coerce")
        located = lats.notna() & lons.notna()
        # A source may geocode only a fraction of its rows — the EM-DAT archive
        # carries coordinates for about one event in ten. A bbox can only ever
        # match a located row, so an unqualified spatial filter silently drops
        # the rest. Say so, rather than returning a suspiciously small table.
        ungeocoded = int((~located).sum())
        if ungeocoded:
            warnings.warn(
                f"{dataset.id}: {ungeocoded} of {len(frame)} row(s) carry no "
                f"coordinates in {lat_col!r}/{lon_col!r} and cannot satisfy a "
                "bounding box, so they were dropped. Omit lat_lim/lon_lim to "
                "keep them, or filter by country= instead.",
                UngeocodedRowsWarning,
                stacklevel=2,
            )
        mask &= (
            lats.between(min_lat, max_lat) & lons.between(min_lon, max_lon)
        ).fillna(False)

    return frame[mask].reset_index(drop=True)


def points_to_feature_collection(
    frame: pd.DataFrame, dataset: Dataset
) -> FeatureCollection:
    """Turn a table carrying coordinates into a point `FeatureCollection`.

    Rows whose latitude or longitude will not parse as a number are dropped —
    a point layer cannot represent them.

    Args:
        frame: The rows to convert.
        dataset: The catalog row naming the coordinate columns.

    Returns:
        FeatureCollection: Point features in `EPSG:4326`, carrying every
            non-coordinate attribute.

    Raises:
        ValueError: If the row does not name both coordinate columns, or the
            table does not carry them.
    """
    import geopandas as gpd
    from pyramids.feature.collection import FeatureCollection

    lat_col, lon_col = dataset.latitude_column, dataset.longitude_column
    if not lat_col or not lon_col or not {lat_col, lon_col} <= set(frame.columns):
        raise ValueError(
            f"{dataset.id} cannot build point features: it names coordinate "
            f"columns {lat_col!r}/{lon_col!r}, which are not both in the table "
            f"(columns present: {list(frame.columns)})."
        )
    lats = pd.to_numeric(frame[lat_col], errors="coerce")
    lons = pd.to_numeric(frame[lon_col], errors="coerce")
    usable = lats.notna() & lons.notna()
    rows = frame[usable].reset_index(drop=True)
    geometry = gpd.points_from_xy(lons[usable], lats[usable])
    gdf = gpd.GeoDataFrame(rows, geometry=gpd.GeoSeries(geometry, crs=CRS), crs=CRS)
    return FeatureCollection(gdf)
