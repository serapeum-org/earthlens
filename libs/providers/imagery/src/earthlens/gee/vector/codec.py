"""`VectorCodec`: convert vector files to `ee.FeatureCollection` and back.

Loading a shapefile / GeoJSON / GeoPackage / KML / parquet into Earth Engine and
writing an `ee.FeatureCollection` back to disk is common glue the backend lacks.
Because `pyramids.feature.FeatureCollection` is a `geopandas.GeoDataFrame`
subclass, the format dispatch is pyramids' job and the codec is a thin pair of
methods:

* `from_file` reads with `FeatureCollection.from_file` and converts to Earth
  Engine through the shared `GeometryConverter`.
* `to_file` pulls an `ee.FeatureCollection` back with the shared
  `FeatureDownloader` (one `getInfo`) and lets pyramids infer the output format
  from the extension.

CSV is the one special case: it carries no intrinsic geometry, so `from_csv`
builds point geometries from named X/Y columns before the same ee conversion.
Injecting the `GeometryConverter` / `FeatureDownloader` keeps the
no-`rasterio`/`fiona`/`xarray`/`xee` policy enforced in one place: every read and
write here goes through a pyramids `FeatureCollection`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import geopandas as gpd
import pandas as pd
from pyramids.feature import FeatureCollection

from earthlens.gee.vector.download import FeatureDownloader
from earthlens.gee.vector.geometry import GeometryConverter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

__all__ = ["VectorCodec"]


class VectorCodec:
    """Read vector files into Earth Engine and write them back out.

    The converter and downloader are injectable so the Earth-Engine and
    download seams can be shared (and faked in tests); both default to a plain
    instance.

    Examples:
        - A codec carries its converter and downloader:
            ```python
            >>> from earthlens.gee.vector.codec import VectorCodec
            >>> from earthlens.gee.vector.geometry import GeometryConverter
            >>> from earthlens.gee.vector.download import FeatureDownloader
            >>> codec = VectorCodec()
            >>> isinstance(codec.converter, GeometryConverter)
            True
            >>> isinstance(codec.downloader, FeatureDownloader)
            True

            ```
    """

    def __init__(
        self,
        *,
        converter: GeometryConverter | None = None,
        downloader: FeatureDownloader | None = None,
    ) -> None:
        """Build a codec over an optional converter and downloader.

        Args:
            converter: The Shapely/`GeoDataFrame` → `ee` bridge. Defaults to a
                plain `GeometryConverter`.
            downloader: The `ee.FeatureCollection` → pyramids bridge. Defaults to
                a plain `FeatureDownloader`.
        """
        self._converter = converter or GeometryConverter()
        self._downloader = downloader or FeatureDownloader()

    @property
    def converter(self) -> GeometryConverter:
        """Return the geometry converter this codec reads through."""
        return self._converter

    @property
    def downloader(self) -> FeatureDownloader:
        """Return the feature downloader this codec writes through."""
        return self._downloader

    def from_file(self, path: str | Path) -> Any:
        """Read a vector file into an `ee.FeatureCollection`.

        `FeatureCollection.from_file` owns the format dispatch
        (shp / geojson / gpkg / kml / parquet); CSV has no intrinsic geometry, so
        use `from_csv` for it.

        Args:
            path: The vector file to read.

        Returns:
            The `ee.FeatureCollection` of the file's features.
        """
        return self._converter.to_ee(FeatureCollection.from_file(path))

    def from_csv(
        self,
        path: str | Path,
        *,
        x: str = "longitude",
        y: str = "latitude",
        crs: int | str = 4326,
    ) -> Any:
        """Read a CSV of point coordinates into an `ee.FeatureCollection`.

        A CSV carries no geometry, so point geometries are built from the `x` /
        `y` columns; every other column rides along as a feature property.

        Args:
            path: The CSV file to read.
            x: The longitude/easting column name. Defaults to `longitude`.
            y: The latitude/northing column name. Defaults to `latitude`.
            crs: The CRS of the coordinates. Defaults to `4326`.

        Returns:
            The `ee.FeatureCollection` of the point features.
        """
        frame = pd.read_csv(path)
        gdf = gpd.GeoDataFrame(
            frame,
            geometry=gpd.points_from_xy(frame[x], frame[y]),
            crs=crs,
        )
        return self._converter.to_ee(FeatureCollection(gdf))

    def to_file(self, fc: Any, path: str | Path) -> None:
        """Write an `ee.FeatureCollection` to disk, format inferred by extension.

        The collection is pulled back with one `getInfo` through the downloader
        (so it is bounded by the same budget), then written by pyramids.

        Args:
            fc: The `ee.FeatureCollection` to write.
            path: The output path; pyramids infers the format from its extension.
        """
        collection = cast("FeatureCollection", self._downloader.to_gdf(fc))
        collection.to_file(path)
