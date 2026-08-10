"""Private, stateless helpers for the Aqueduct backend.

Three concerns live here, kept out of `backend.py` so the
:class:`~earthlens.aqueduct.backend.Aqueduct` class stays a thin orchestration
layer: the **column grammar** (turning a metric / year / scenario /
return-period selection into the `.dbf` column names to read), the **zip IO**
(pulling one shapefile — and its sidecars — out of a downloaded zip, including
the doubly-nested `state` bundle), and the **FeatureCollection assembly**
(selecting + renaming the chosen columns and applying the unit-name / bbox
filter). All geometry handling stays inside pyramids
(:class:`~pyramids.feature.collection.FeatureCollection`, itself a
`geopandas.GeoDataFrame` subclass); this module never imports `xarray`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pyramids.feature.collection import FeatureCollection

if TYPE_CHECKING:
    from earthlens.aqueduct.catalog import AdminLevel, Catalog
    from earthlens.base import SpatialExtent

#: WGS84 — the CRS every Aqueduct shapefile is tagged with.
FEATURE_CRS = "EPSG:4326"

#: The shapefile sidecar members extracted alongside the `.shp`.
_SHAPEFILE_EXTENSIONS = (".shp", ".dbf", ".shx", ".prj", ".cpg")

#: Non-value identity columns kept on every returned FeatureCollection.
IDENTITY_COLUMNS = ("unit_id", "unit_name")


def resolve_columns(
    catalog: Catalog,
    metric: str,
    year: str,
    scenario: str,
    return_periods: list[int],
) -> dict[int, str]:
    """Map each requested return period to its `.dbf` column name.

    A column name is `f"{indicator}{year}_{scenario}_{rp}"` — e.g.
    `population_affected` + `2030` + `ssp2-rcp8p5` + `100` yr -> `"P30_28_100"`.
    Every selector is validated against the catalog vocabularies, and the
    `scenario`/`year` pairing is enforced (the 2010 baseline has no future
    scenario, and a 2030 scenario has no 2010 column).

    Args:
        catalog: The loaded :class:`~earthlens.aqueduct.catalog.Catalog`.
        metric: A public indicator name (`"gdp_affected"` /
            `"population_affected"` / `"urban_damage"`).
        year: `"2010"` or `"2030"`.
        scenario: A scenario name (`"baseline"`, `"ssp2-rcp8p5"`, ...).
        return_periods: The flood return periods (years) to select.

    Returns:
        dict[int, str]: `{return_period: column_name}` in the given order.

    Raises:
        ValueError: If any selector is unknown, if `scenario` is not valid for
            `year`, or if a return period is not one of the shipped nine.

    Examples:
        - Resolve a 2030 population selection to its `.dbf` column names:
            ```python
            >>> from earthlens.aqueduct import Catalog
            >>> from earthlens.aqueduct._helpers import resolve_columns
            >>> resolve_columns(
            ...     Catalog(), "population_affected", "2030", "ssp2-rcp8p5", [100, 1000]
            ... )
            {100: 'P30_28_100', 1000: 'P30_28_1T'}

            ```
        - The 2010 baseline uses the `baseline` scenario:
            ```python
            >>> from earthlens.aqueduct import Catalog
            >>> from earthlens.aqueduct._helpers import resolve_columns
            >>> resolve_columns(Catalog(), "gdp_affected", "2010", "baseline", [100])
            {100: 'G10_bh_100'}

            ```
    """
    indicator_code = _lookup(catalog.indicators, metric, "metric")
    year_code = _lookup(catalog.years, year, "year")
    if scenario not in catalog.scenarios:
        raise ValueError(
            f"scenario {scenario!r} is not in the Aqueduct catalog. Known "
            f"scenarios: {sorted(catalog.scenarios)}."
        )
    scenario_row = catalog.scenarios[scenario]
    if year not in scenario_row.years:
        raise ValueError(
            f"scenario {scenario!r} is not defined for year {year!r} (valid "
            f"years: {scenario_row.years}). The 2010 baseline uses "
            "scenario='baseline'; the 2030 futures are the RCP*SSP combinations."
        )
    columns: dict[int, str] = {}
    for rp in return_periods:
        if rp not in catalog.return_periods:
            raise ValueError(
                f"return_period {rp!r} is not one of the shipped return periods "
                f"{sorted(catalog.return_periods)} (years)."
            )
        rp_code = catalog.return_periods[rp]
        columns[rp] = f"{indicator_code}{year_code}_{scenario_row.code}_{rp_code}"
    return columns


def _lookup(mapping: dict[str, str], key: str, label: str) -> str:
    """Return `mapping[key]`, raising a listing `ValueError` when absent.

    Args:
        mapping: A catalog vocabulary (indicator / year code map).
        key: The requested public name.
        label: The selector name used in the error message.

    Returns:
        str: The mapped code.

    Raises:
        ValueError: If `key` is not in `mapping`.
    """
    if key not in mapping:
        raise ValueError(
            f"{label} {key!r} is not in the Aqueduct catalog. Known {label}s: "
            f"{sorted(mapping)}."
        )
    return mapping[key]


def extract_shapefile(zip_path: Path, row: AdminLevel, dest_dir: Path) -> Path:
    """Extract one admin level's shapefile (+ sidecars) from a downloaded zip.

    Handles the two shipped shapes: a direct zip whose members are the
    shapefile itself, and the `state` bundle whose members are per-level
    **inner** zips (the shapefile lives inside `row.zip`, itself inside the
    downloaded `row.container_zip`).

    Args:
        zip_path: The downloaded zip on disk (the direct zip, or the outer
            bundle for a nested level).
        row: The admin level's catalog spec.
        dest_dir: Directory to write the extracted `.shp` + sidecars into.

    Returns:
        Path: The extracted `.shp` path.

    Raises:
        FileNotFoundError: If the expected inner zip or `.shp` member is absent.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as outer:
        if row.container_zip is not None:
            inner_name = _member_ending(outer, row.zip)
            with outer.open(inner_name) as handle, zipfile.ZipFile(handle) as inner:
                return _extract_members(inner, row.shapefile_stem, dest_dir)
        return _extract_members(outer, row.shapefile_stem, dest_dir)


def _member_ending(archive: zipfile.ZipFile, suffix: str) -> str:
    """Return the archive member whose name ends with `suffix`.

    Args:
        archive: An open zip.
        suffix: The member file name to find (matched on the base name).

    Returns:
        str: The full member path inside the archive.

    Raises:
        FileNotFoundError: If no member matches.
    """
    for name in archive.namelist():
        if name.split("/")[-1] == suffix:
            return name
    raise FileNotFoundError(
        f"{suffix!r} is not a member of the archive (members: {archive.namelist()})."
    )


def _extract_members(archive: zipfile.ZipFile, stem: str, dest_dir: Path) -> Path:
    """Extract `{stem}.{ext}` for each shapefile sidecar present in `archive`.

    Args:
        archive: An open zip holding the shapefile members.
        stem: The shapefile stem (no extension).
        dest_dir: Where to write the extracted members.

    Returns:
        Path: The extracted `.shp` path.

    Raises:
        FileNotFoundError: If the `.shp` member is absent.
    """
    members = {name.split("/")[-1]: name for name in archive.namelist()}
    shp_path: Path | None = None
    for ext in _SHAPEFILE_EXTENSIONS:
        member = members.get(f"{stem}{ext}")
        if member is None:
            continue
        out = dest_dir / f"{stem}{ext}"
        out.write_bytes(archive.read(member))
        if ext == ".shp":
            shp_path = out
    if shp_path is None:
        raise FileNotFoundError(
            f"{stem}.shp is not a member of the archive (members: {sorted(members)})."
        )
    return shp_path


def build_feature_collection(
    source: FeatureCollection,
    columns: dict[int, str],
) -> FeatureCollection:
    """Select the identity + chosen value columns and rename them to `rp_<n>`.

    The returned collection carries `unit_id`, `unit_name`, one `rp_<years>`
    column per selected return period (e.g. `rp_100`), and the geometry — the
    per-return-period exposure for the requested metric / year / scenario.

    Args:
        source: The full shapefile read as a
            :class:`~pyramids.feature.collection.FeatureCollection`.
        columns: `{return_period: source_column_name}` from
            :func:`resolve_columns`.

    Returns:
        FeatureCollection: The trimmed, renamed collection, CRS `EPSG:4326`.

    Raises:
        ValueError: If the shapefile lacks an expected identity or value column
            (a clean domain error rather than a raw pandas `KeyError`), listing
            what is available.
    """
    required = [*IDENTITY_COLUMNS, *columns.values()]
    missing = [column for column in required if column not in source.columns]
    if missing:
        raise ValueError(
            f"the Aqueduct shapefile is missing expected column(s) {missing}; "
            f"available columns: {sorted(source.columns)}."
        )
    rename = {name: f"rp_{rp}" for rp, name in columns.items()}
    keep = [*IDENTITY_COLUMNS, *columns.values(), source.geometry.name]
    trimmed = source[keep].rename(columns=rename)
    return FeatureCollection(trimmed.set_crs(FEATURE_CRS, allow_override=True))


def filter_units(
    collection: FeatureCollection,
    country: str | None,
    space: SpatialExtent,
) -> FeatureCollection:
    """Filter the collection by unit name and/or the requested bounding box.

    A `country` keeps rows whose `unit_name` matches case-insensitively — at
    country level `unit_name` *is* the country name. (The state layer also carries
    an `admin` country column and the basin layer none, but this backend filters
    `country=` on `unit_name` only; use the bounding box to select a sub-national
    region.) A bounding box narrower than the whole globe keeps rows whose
    geometry intersects it (via `GeoDataFrame.cx`). Both filters compose.

    Args:
        collection: The trimmed collection from :func:`build_feature_collection`.
        country: A unit name to match (case-insensitive exact), or `None`.
        space: The requested :class:`~earthlens.base.SpatialExtent`; a
            whole-globe extent applies no spatial filter.

    Returns:
        FeatureCollection: The filtered collection, CRS `EPSG:4326`.
    """
    result = collection
    if country is not None:
        target = country.strip().casefold()
        result = result[result["unit_name"].str.strip().str.casefold() == target]
        if result.empty:
            logger.warning(
                f"Aqueduct: country={country!r} matched no unit_name (the match "
                "is exact, case-insensitive, against the source spelling). "
                "Check the spelling/diacritics, or filter by bbox instead."
            )
    if not _is_global(space):
        result = result.cx[
            space.longitude_min : space.longitude_max,
            space.latitude_min : space.latitude_max,
        ]
    return FeatureCollection(result.set_crs(FEATURE_CRS, allow_override=True))


def _is_global(space: SpatialExtent) -> bool:
    """Return whether `space` is (effectively) the whole globe — no bbox filter.

    Args:
        space: The requested spatial extent.

    Returns:
        bool: `True` when the box spans the full WGS84 range.
    """
    return (
        space.latitude_min <= -90.0
        and space.latitude_max >= 90.0
        and space.longitude_min <= -180.0
        and space.longitude_max >= 180.0
    )
