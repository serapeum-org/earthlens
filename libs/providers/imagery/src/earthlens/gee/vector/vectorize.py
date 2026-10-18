"""`Vectorize`: turn a categorical / thresholded raster into polygons.

Task 3.9's `VectorCodec` carries vector data `ee` <-> file; nothing goes
raster -> vector. `Vectorize` does, so a classification (Tasks 2.9/2.10) or a
threshold mask (flood extent, water occurrence, burn scar) comes back as the
polygon, not the raster:

* **server** - `ee.Image.reduceToVectors`, so the vectorisation happens where the
  pixels are rather than downloading a large classified raster to polygonise
  locally.
* **client** - pyramids' own `Dataset.to_polygons`, for a raster already in
  memory.

It is neither a `Sampler` (which takes an image *and* a geometry) nor a
`RasterOperation` (`Dataset -> Dataset`): it consumes a raster and emits vector,
so it is its own small converter, a frozen value object carrying the
`reduceToVectors` options.

A synchronous fetch is bounded by Earth Engine's `getInfo` ceiling (~5000
features); a vectorisation expected to exceed `SYNC_FETCH_LIMIT` polygons should
be routed through a `TableSink` (the Epic-3 batch vector path) instead of
`to_gdf`. Pair it with `Sieve` (Task 2.13): vectorising an un-sieved
classification produces thousands of single-pixel polygons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from earthlens.gee.reducers import resolve_reducer

if TYPE_CHECKING:  # pragma: no cover - typing only
    import ee
    from pyramids.dataset import Dataset
    from pyramids.feature import FeatureCollection

__all__ = ["SYNC_FETCH_LIMIT", "Vectorize"]

#: The `getInfo` feature ceiling; a vectorisation expected to exceed it should
#: route through a `TableSink` rather than a synchronous `to_gdf`.
SYNC_FETCH_LIMIT: int = 5000


class Vectorize(BaseModel):
    """Convert a raster to polygons, server-side (`ee`) or client-side (pyramids).

    `to_ee` / `to_gdf` run `ee.Image.reduceToVectors` in Earth Engine;
    `to_polygons` reduces an in-memory pyramids `Dataset` locally. Both carry the
    class value as `label_property`.

    Examples:
        - A vectoriser carries its geometry type and connectedness:
            ```python
            >>> from earthlens.gee.vector.vectorize import Vectorize
            >>> vec = Vectorize(scale_m=30, geometry_type="polygon", eight_connected=False)
            >>> (vec.geometry_type, vec.eight_connected, vec.label_property)
            ('polygon', False, 'label')

            ```
        - An unknown option is refused at construction:
            ```python
            >>> from earthlens.gee.vector.vectorize import Vectorize
            >>> Vectorize(scale_m=30, geometryType="polygon")
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale_m: float  # pixel scale (metres) at which to vectorise
    geometry_type: Literal["polygon", "centroid", "bb"] = "polygon"  # output shape
    eight_connected: bool = True  # diagonally-touching pixels join one polygon
    label_property: str = "label"  # property carrying each polygon's class value
    reducer: str | None = None  # optional registry reducer attached per polygon
    max_pixels: int = 100_000_000  # reduceToVectors pixel budget

    def to_ee(self, image: ee.Image) -> ee.FeatureCollection:
        """Vectorise `image` server-side with `reduceToVectors`.

        Args:
            image: The categorical / thresholded `ee.Image` to vectorise.

        Returns:
            An `ee.FeatureCollection` of polygons (or centroids / boxes), each
            carrying its class value under `label_property`.
        """
        options: dict[str, Any] = {
            "geometryType": self.geometry_type,
            "eightConnected": self.eight_connected,
            "labelProperty": self.label_property,
            "scale": self.scale_m,
            "maxPixels": self.max_pixels,
        }
        if self.reducer is not None:
            options["reducer"] = resolve_reducer(self.reducer).as_ee_reducer()
        return cast("ee.FeatureCollection", image.reduceToVectors(**options))

    def to_gdf(self, image: ee.Image, *, crs: int | str = 4326) -> FeatureCollection:
        """Vectorise server-side and pull the polygons into a `FeatureCollection`.

        Suitable while the result stays under `SYNC_FETCH_LIMIT`; a larger
        vectorisation should be exported through a `TableSink` instead.

        Args:
            image: The `ee.Image` to vectorise.
            crs: CRS stamped on the returned collection. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection` of the vectorised polygons.
        """
        from earthlens.gee.vector.download import FeatureDownloader

        return FeatureDownloader().to_gdf(self.to_ee(image), crs=crs)

    def to_polygons(self, dataset: Dataset, *, band: int = 0) -> FeatureCollection:
        """Vectorise an in-memory pyramids `Dataset` client-side.

        Args:
            dataset: The raster to polygonise, already in memory.
            band: The band to vectorise. Defaults to `0`.

        Returns:
            A pyramids `FeatureCollection` of the polygons.
        """
        from pyramids.feature import FeatureCollection

        return cast("FeatureCollection", FeatureCollection(dataset.to_polygons(band)))
