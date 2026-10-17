"""Distance from every cell to the nearest target cell.

A thin wrapper over `Dataset.proximity`, which is pyramids' facade over
`gdal.ComputeProximity` - the same primitive `gdal_proximity` exposes, and the
client-side counterpart of `ee.Image.distance`. Distance-to-coast,
distance-to-river and buffer analyses are all this operation with a different
`target_values`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import field_validator

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["Proximity"]


class Proximity(RasterOperation):
    """Per-cell distance to the closest target cell.

    With `target_values` left unset, every non-zero cell is a target, which is
    GDAL's own default and the right one for a mask raster. Distances are in the
    CRS's units unless `distance_units="PIXEL"`, so a geographic raster reports
    degrees - worth knowing before comparing a result against metres.

    Examples:
        - Distance to a single target cell, measured in pixels:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> mask = np.zeros((3, 3), dtype="int16")
            >>> mask[1, 1] = 1
            >>> raster = Dataset.from_array(
            ...     arr=mask,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-1,
            ... )
            >>> out = np.asarray(Proximity(distance_units="PIXEL")(raster).read_array())
            >>> float(out[1, 1]), float(out[0, 1])
            (0.0, 1.0)

            ```
    """

    band: int = 0
    target_values: tuple[int, ...] | None = None
    distance_units: Literal["GEO", "PIXEL"] = "GEO"
    max_distance: float | None = None
    nodata: float | None = None

    @field_validator("target_values")
    @classmethod
    def _targets_must_not_be_empty(
        cls, value: tuple[int, ...] | None
    ) -> tuple[int, ...] | None:
        """Refuse an empty tuple, which read as the opposite instruction.

        `target_values=()` is falsy, so it fell through to GDAL's default of
        "every non-zero cell is a target" - measured as output identical to
        passing nothing at all. An explicit empty collection is more likely a
        filter that was computed and came back empty, and measuring distance to
        everything is the worst available answer to that.

        Args:
            value: The configured target values.

        Returns:
            The value unchanged, when it is usable.

        Raises:
            ValueError: If an empty collection was given.
        """
        if value is not None and not value:
            raise ValueError(
                "target_values is empty; pass None to treat every non-zero "
                "cell as a target, or name the values to measure distance to"
            )
        return value

    def apply(self, ds: Dataset) -> Dataset:
        """Return the distance raster.

        With `max_distance` set, cells further than the cap are not left
        undefined: GDAL fills them with `65535` unless `nodata` says otherwise.
        Measured on a capped run, which is why `nodata` is exposed here - a
        reader that takes `65535` for a distance would be badly wrong, and the
        value is far enough from any real distance to look deliberate.

        Args:
            ds: The source raster, whose target cells define distance zero.

        Returns:
            A new raster of distances in the configured units.
        """
        return ds.proximity(
            band=self.band,
            # pyramids takes a list; the field is a tuple so the operation can
            # stay frozen and hashable.
            target_values=(
                list(self.target_values) if self.target_values is not None else None
            ),
            distance_units=self.distance_units,
            max_distance=self.max_distance,
            nodata=self.nodata,
        )
