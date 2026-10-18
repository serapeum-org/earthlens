"""`ZonalHistogram`: per-zone class counts and areas for a categorical raster.

Task 3.3's `RegionSampler` reduces a *continuous* band over a zone (mean, min,
...); the mean of land-cover class codes is meaningless, so a categorical raster
needs a different question answered: how many pixels - and how much ground area -
of each class fall inside each zone ("how much forest is in each catchment").

`ZonalHistogram` answers it server-side with one grouped `reduceRegions`: the
class band is grouped and, per class, both the pixel `count` and the summed
`ee.Image.pixelArea()` come back. Area is read from `pixelArea`, never a constant,
so it stays correct on a geographic CRS where a pixel's ground area changes with
latitude. `sample_to_gdf` then widens the per-zone groups into one column per
class, applying the requested `normalize` (raw `count`, `fraction` of the zone,
or `area` in m^2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import ee
from pydantic import field_validator

from earthlens.gee.sampling.base import Sampler
from earthlens.gee.vector.geometry import GeometryConverter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from geopandas.geodataframe import GeoDataFrame
    from pyramids.feature import FeatureCollection

__all__ = ["ZonalHistogram"]


class ZonalHistogram(Sampler):
    """Per-zone, per-class pixel counts and areas for a categorical raster.

    `apply` the sampler to a categorical `image` and a polygon `GeoDataFrame`; it
    reduces the `class_band` over each zone and returns one row per zone with a
    column per observed class. `normalize` selects what the column holds: the raw
    pixel `count`, the `fraction` of the zone (columns sum to 1), or the ground
    `area` in square metres (summed from `ee.Image.pixelArea()`, correct on a
    geographic CRS). Class columns are labelled from `class_labels` when given,
    otherwise by the raw integer code.

    Args:
        scale_m: The pixel scale (metres) for the reduction.
        class_band: The categorical band whose classes are counted per zone.
        normalize: `"count"` (pixels), `"fraction"` (of the zone), or `"area"`
            (m^2). Defaults to `"count"`.
        class_labels: Maps a class code to a column name; unmapped codes use the
            raw code as a string. Defaults to `None` (all raw codes).
        tile_scale: Earth Engine's `tileScale`; raise it (2 or 4) to let an
            out-of-memory aggregation over a large zone layer succeed. Must be
            within `0.1`-`16`. Defaults to `1.0`.

    Examples:
        - A zonal histogram carries its class band and normalisation:
            ```python
            >>> from earthlens.gee.sampling.histogram import ZonalHistogram
            >>> hist = ZonalHistogram(scale_m=30, class_band="landcover", normalize="area")
            >>> (hist.class_band, hist.normalize, hist.tile_scale)
            ('landcover', 'area', 1.0)

            ```
        - `tile_scale` is validated against Earth Engine's documented range:
            ```python
            >>> from earthlens.gee.sampling.histogram import ZonalHistogram
            >>> ZonalHistogram(scale_m=30, class_band="lc", tile_scale=0)
            Traceback (most recent call last):
                ...
            pydantic_core._pydantic_core.ValidationError: ...

            ```
    """

    _POLYGON_GEOM_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"Polygon", "MultiPolygon"}
    )

    class_band: str
    normalize: Literal["count", "fraction", "area"] = "count"
    class_labels: dict[int, str] | None = None
    tile_scale: float = 1.0

    @field_validator("tile_scale")
    @classmethod
    def _check_tile_scale(cls, value: float) -> float:
        """Keep `tile_scale` within Earth Engine's documented `0.1`-`16`.

        Args:
            value: The requested tile scale.

        Returns:
            `value` unchanged once in range.

        Raises:
            ValueError: If `value` is outside `[0.1, 16]`.
        """
        if not 0.1 <= value <= 16.0:
            raise ValueError(f"tile_scale must be within [0.1, 16], got {value}")
        return value

    def sample(self, image: ee.Image, geometry: GeoDataFrame) -> ee.FeatureCollection:
        """Group the class band per zone, returning per-class count and area.

        Args:
            image: The categorical `ee.Image`; its `class_band` is grouped.
            geometry: A `GeoDataFrame` of `Polygon` / `MultiPolygon` zones.

        Returns:
            An `ee.FeatureCollection` of the zones, each carrying a `groups` list
            of `{class, sum, count}` entries (summed `pixelArea`, pixel count).

        Raises:
            ValueError: If `geometry` is empty or holds a non-polygon geometry.
        """
        self._validate_geometry(geometry, self._POLYGON_GEOM_TYPES)
        stacked = ee.Image.pixelArea().addBands(image.select([self.class_band]))
        reducer = (
            ee.Reducer.sum()
            .combine(reducer2=ee.Reducer.count(), sharedInputs=True)
            .group(groupField=1, groupName="class")
        )
        collection = stacked.reduceRegions(
            collection=GeometryConverter().to_ee(geometry),
            reducer=reducer,
            scale=self.scale_m,
            tileScale=self.tile_scale,
        )
        return cast("ee.FeatureCollection", collection)

    def sample_to_gdf(
        self, image: ee.Image, geometry: GeoDataFrame, *, crs: int | str = 4326
    ) -> FeatureCollection:
        """Return one row per zone, one column per class under `normalize`.

        Args:
            image: The categorical `ee.Image` to reduce.
            geometry: A `GeoDataFrame` of polygon zones.
            crs: CRS stamped on the returned collection. Defaults to `4326`.

        Returns:
            A pyramids `FeatureCollection`: one row per zone, a column per class.
        """
        from pyramids.feature import FeatureCollection

        from earthlens.gee.vector.download import FeatureDownloader

        raw = FeatureDownloader().to_gdf(self.sample(image, geometry), crs=crs)
        return cast("FeatureCollection", FeatureCollection(self._widen(raw)))

    def _label(self, code: int) -> str:
        """Name a class column from `class_labels`, else the raw code.

        Args:
            code: The integer class value.

        Returns:
            The mapped label, or the code as a string when unmapped.
        """
        return (self.class_labels or {}).get(code, str(code))

    def _value(self, group: dict[str, Any], total: float) -> float:
        """Pick the column value for one class group under `normalize`.

        Args:
            group: One `{class, sum, count}` entry.
            total: The zone's total pixel count, for `"fraction"`.

        Returns:
            The pixel count, the fraction of the zone, or the area in m^2.
        """
        if self.normalize == "count":
            result = float(group["count"])
        elif self.normalize == "fraction":
            result = float(group["count"]) / total
        else:
            result = float(group["sum"])
        return result

    def _widen(self, gdf: GeoDataFrame) -> GeoDataFrame:
        """Widen each zone's `groups` list into one column per class.

        Args:
            gdf: The downloaded zones, each with a `groups` list property.

        Returns:
            A `GeoDataFrame` with the `groups` column replaced by per-class
            columns (`0` where a class is absent from a zone).
        """
        import pandas as pd

        per_zone: list[dict[str, float]] = []
        for groups in gdf["groups"]:
            entries = groups or []
            total = float(sum(entry["count"] for entry in entries)) or 1.0
            per_zone.append(
                {self._label(int(e["class"])): self._value(e, total) for e in entries}
            )
        wide = pd.DataFrame(per_zone, index=gdf.index).fillna(0.0)
        return gdf.drop(columns=["groups"]).join(wide)
