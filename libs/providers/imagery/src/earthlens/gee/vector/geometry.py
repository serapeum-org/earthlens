"""Shapely / `GeoDataFrame` -> Earth Engine geometry conversion, as a class.

`GeometryConverter` holds the EPSG code and the optional property-column
selection once, so every sampler in this subpackage shares one configured
converter instead of threading `epsg` / `columns` through each call. It is the
class home for the `LineString` support the transect sampler needs: the free
`earthlens.gee.features.create_geometry` keeps raising `NotImplementedError` for
a line (its documented contract), while `GeometryConverter.to_ee_geometry`
accepts one.

Because `pyramids.feature.FeatureCollection` subclasses
`geopandas.GeoDataFrame`, `to_ee` accepts a pyramids `FeatureCollection`
directly, with no adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import ee
import pandas as pd
from ee.featurecollection import FeatureCollection
from ee.geometry import Geometry
from pydantic import BaseModel, ConfigDict
from shapely.geometry.base import BaseGeometry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas.geodataframe import GeoDataFrame

__all__ = ["GeometryConverter", "GeometryConverterConfig"]


class GeometryConverterConfig(BaseModel):
    """Frozen configuration shared by a `GeometryConverter`.

    Attributes:
        epsg: EPSG code of the input coordinates, used to build the
            `"epsg:<code>"` projection string handed to Earth Engine.
        columns: When given, only these non-geometry columns become feature
            properties; `None` keeps every non-geometry column.

    Examples:
        - The default is WGS84 with every column kept:
            ```python
            >>> from earthlens.gee.vector.geometry import GeometryConverterConfig
            >>> cfg = GeometryConverterConfig()
            >>> cfg.epsg
            4326
            >>> cfg.columns is None
            True

            ```
        - It is frozen, so a converter's configuration cannot drift:
            ```python
            >>> from earthlens.gee.vector.geometry import GeometryConverterConfig
            >>> cfg = GeometryConverterConfig(epsg=3857, columns=("name",))
            >>> cfg.epsg
            3857
            >>> cfg.columns
            ('name',)

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    epsg: int = 4326
    columns: tuple[str, ...] | None = None


class GeometryConverter:
    """Convert Shapely geometries / `GeoDataFrame`s into Earth Engine objects.

    A converter carries a `GeometryConverterConfig` (its EPSG code and optional
    column selection) and exposes two conversions: `to_ee_geometry` for a single
    Shapely geometry, and `to_ee` for a whole `GeoDataFrame` (or pyramids
    `FeatureCollection`). Unlike the free `create_geometry`, it supports
    `Polygon`, `Point` **and** `LineString`.

    Examples:
        - A converter exposes its configured EPSG:
            ```python
            >>> from earthlens.gee.vector.geometry import GeometryConverter
            >>> GeometryConverter(epsg=3857).epsg
            3857

            ```
        - A narrowed column selection is remembered:
            ```python
            >>> from earthlens.gee.vector.geometry import GeometryConverter
            >>> GeometryConverter(columns=["name"]).columns
            ('name',)

            ```
    """

    def __init__(self, *, epsg: int = 4326, columns: list[str] | None = None) -> None:
        """Build a converter.

        Args:
            epsg: EPSG code of the input coordinates. Defaults to `4326`.
            columns: When given, only these non-geometry columns become feature
                properties. Defaults to `None` (keep every column).
        """
        self._config = GeometryConverterConfig(
            epsg=epsg, columns=tuple(columns) if columns is not None else None
        )

    @property
    def epsg(self) -> int:
        """Return the EPSG code this converter projects coordinates from."""
        return self._config.epsg

    @property
    def columns(self) -> tuple[str, ...] | None:
        """Return the configured property-column selection, or `None` for all."""
        return self._config.columns

    def to_ee_geometry(self, shapely_geometry: BaseGeometry) -> Geometry:
        """Convert one Shapely geometry to an `ee.Geometry`.

        Args:
            shapely_geometry: A Shapely `Polygon`, `Point` or `LineString`; its
                GeoJSON coordinates are passed to the matching `ee.Geometry`
                factory with this converter's `"epsg:<code>"` projection.

        Returns:
            The corresponding `ee.Geometry`.

        Raises:
            NotImplementedError: If the geometry is any other type (e.g.
                `MultiPolygon`, `MultiPoint`); `to_ee` explodes a `MultiPolygon`
                into per-polygon rows before reaching this.
        """
        coords = shapely_geometry.__geo_interface__["coordinates"]
        geom_type = shapely_geometry.geom_type
        proj = f"epsg:{self._config.epsg}"
        if geom_type == "Polygon":
            geometry = ee.Geometry.Polygon(coords, proj)
        elif geom_type == "Point":
            geometry = ee.Geometry.Point(coords, proj)
        elif geom_type == "LineString":
            geometry = ee.Geometry.LineString(coords, proj)
        else:
            raise NotImplementedError(
                f"{geom_type} geometries are not supported by the GEE backend; "
                "only Polygon, Point and LineString are accepted (MultiPolygon "
                "is auto-exploded by to_ee)."
            )
        return cast("Geometry", geometry)

    def to_ee(self, gdf: GeoDataFrame) -> FeatureCollection:
        """Build an `ee.FeatureCollection` from a `GeoDataFrame`.

        A row holding a `MultiPolygon` is exploded into one feature per
        constituent polygon; every other row converts through `to_ee_geometry`.
        The non-geometry columns (narrowed to this converter's `columns`, if set)
        become each feature's property dictionary.

        Args:
            gdf: A `GeoDataFrame` (or pyramids `FeatureCollection`, which is one)
                whose `geometry` column holds `Polygon` / `Point` / `LineString`
                / `MultiPolygon` geometries.

        Returns:
            An `ee.FeatureCollection` with one feature per (exploded) row.

        Raises:
            ValueError: If a row's geometry cannot be converted (naming the row).
            KeyError: If `gdf` has no `geometry` column or a requested column is
                missing.
        """
        geom_types = [geom.geom_type for geom in gdf["geometry"]]
        if "MultiPolygon" in geom_types:
            gdf = gdf.explode(index_parts=True)
        ee_geometries: list[Geometry] = []
        for row_index, geom in enumerate(gdf.geometry):
            try:
                ee_geometries.append(self.to_ee_geometry(geom))
            except NotImplementedError as exc:
                raise ValueError(
                    f"cannot convert row {row_index} ({geom.geom_type}): {exc}"
                ) from exc
        records_df = pd.DataFrame(gdf.drop("geometry", axis=1))
        if self._config.columns:
            records_df = records_df[list(self._config.columns)]
        records = records_df.to_dict("records")
        if not records:
            features = [ee.Feature(geometry) for geometry in ee_geometries]
        else:
            features = [
                ee.Feature(geometry, record)
                for geometry, record in zip(ee_geometries, records, strict=True)
            ]
        return cast("FeatureCollection", ee.FeatureCollection(features))
