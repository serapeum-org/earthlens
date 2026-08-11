"""Private, stateless helpers for the FLOPROS backend.

Three concerns live here so :class:`~earthlens.flopros.backend.FLOPROS` stays a
thin orchestration layer: the **layer grammar** (turning a `layer=` selection
into the source `.dbf` columns to read and their public names), the **zip IO**
(pulling the FLOPROS shapefile — and its sidecars — out of the downloaded NHESS
supplement zip via the shared :func:`earthlens.base.archive.extract_members`),
and the **FeatureCollection assembly** (selecting + renaming the identity and
chosen layer columns and applying the unit-name / bbox filter). All geometry
handling stays inside pyramids
(:class:`~pyramids.feature.collection.FeatureCollection`, itself a
`geopandas.GeoDataFrame` subclass); this module never imports `xarray`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pyramids.feature.collection import FeatureCollection

from earthlens.base.archive import extract_members

if TYPE_CHECKING:
    from earthlens.base import SpatialExtent
    from earthlens.flopros.catalog import FloprosDataset

#: WGS84 — the CRS the FLOPROS shapefile is tagged with.
FEATURE_CRS = "EPSG:4326"

#: The shapefile members extracted alongside the `.shp` so GDAL can read it.
_SHAPEFILE_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


def resolve_layers(
    dataset: FloprosDataset, layer: str | list[str] | None
) -> dict[str, str]:
    """Map each requested public layer name to its source `.dbf` column.

    Args:
        dataset: The resolved FLOPROS catalog row (carries the `layers` map).
        layer: A public layer name, a list of them, or `None` for every layer
            in catalog order.

    Returns:
        dict[str, str]: `{public_name: source_column}` in the requested order.

    Raises:
        ValueError: If a requested layer name is not in the catalog.

    Examples:
        - Resolve one layer to its source column:
            ```python
            >>> from earthlens.flopros import Catalog
            >>> from earthlens.flopros._helpers import resolve_layers
            >>> resolve_layers(Catalog().get("flopros"), "merged_riverine")
            {'merged_riverine': 'MerL_Riv'}

            ```
    """
    if layer is None:
        return dict(dataset.layers)
    names = [layer] if isinstance(layer, str) else list(dict.fromkeys(layer))
    resolved: dict[str, str] = {}
    for name in names:
        if name not in dataset.layers:
            raise ValueError(
                f"layer {name!r} is not a FLOPROS layer. Known layers: "
                f"{sorted(dataset.layers)}."
            )
        resolved[name] = dataset.layers[name]
    return resolved


def extract_shapefile(zip_path: Path, stem: str, dest_dir: Path) -> Path:
    """Extract the FLOPROS shapefile (+ sidecars) from the supplement zip.

    Uses the shared :func:`earthlens.base.archive.extract_members` (which guards
    against Zip-Slip) to pull every `{stem}.*` shapefile member, then returns
    the extracted `.shp`.

    Args:
        zip_path: The downloaded supplement zip on disk.
        stem: The shapefile stem to extract (e.g. `"FLOPROS_shp_V1"`).
        dest_dir: Directory to extract the members into.

    Returns:
        Path: The extracted `.shp` path.

    Raises:
        FileNotFoundError: If no `.shp` member for `stem` is present.
    """
    extracted = extract_members(zip_path, dest_dir, include=_SHAPEFILE_EXTENSIONS)
    for path in extracted:
        if path.name == f"{stem}.shp":
            return path
    raise FileNotFoundError(
        f"{stem}.shp is not a member of {zip_path.name} "
        f"(extracted: {sorted(p.name for p in extracted)})."
    )


def build_feature_collection(
    source: FeatureCollection,
    identity_columns: list[str],
    layers: dict[str, str],
) -> FeatureCollection:
    """Select the identity + chosen layer columns and rename layers to public names.

    Args:
        source: The full FLOPROS shapefile read as a
            :class:`~pyramids.feature.collection.FeatureCollection`.
        identity_columns: The identity columns to keep (`name` / `geonunit` /
            `type_en`).
        layers: `{public_name: source_column}` from :func:`resolve_layers`.

    Returns:
        FeatureCollection: The trimmed, renamed collection, CRS `EPSG:4326`.

    Raises:
        ValueError: If the shapefile lacks an expected identity or layer column
            (a clean domain error rather than a raw pandas `KeyError`), listing
            what is available.
    """
    required = [*identity_columns, *layers.values()]
    missing = [column for column in required if column not in source.columns]
    if missing:
        raise ValueError(
            f"the FLOPROS shapefile is missing expected column(s) {missing}; "
            f"available columns: {sorted(source.columns)}."
        )
    rename = {column: name for name, column in layers.items()}
    keep = [*identity_columns, *layers.values(), source.geometry.name]
    trimmed = source[keep].rename(columns=rename)
    return FeatureCollection(trimmed.set_crs(FEATURE_CRS, allow_override=True))


def filter_units(
    collection: FeatureCollection,
    country: str | None,
    space: SpatialExtent,
) -> FeatureCollection:
    """Filter the collection by unit name and/or the requested bounding box.

    A `country` keeps rows whose `name` **or** `geonunit` matches
    case-insensitively (FLOPROS units are national or subnational). A bounding
    box narrower than the whole globe keeps rows whose geometry intersects it
    (via `GeoDataFrame.cx`). Both filters compose.

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
        keys = [c for c in ("name", "geonunit") if c in result.columns]
        mask = None
        for column in keys:
            column_mask = result[column].fillna("").str.strip().str.casefold() == target
            mask = column_mask if mask is None else (mask | column_mask)
        if mask is not None:
            result = result[mask]
        if result.empty:
            logger.warning(
                f"FLOPROS: country={country!r} matched no name/geonunit (the "
                "match is exact, case-insensitive). Check the spelling, or "
                "filter by bbox instead."
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
