"""Patch analysis: label contiguous regions, and drop the ones too small to keep.

These are the two operations a classified raster needs after the fact.
`ConnectedComponents` is Earth Engine's `connectedComponents` client-side, and
`Sieve` is `gdal_sieve` - the speckle removal that makes a per-pixel
classification readable.

Both delegate. pyramids carries the primitives already, under one name that is
easy to miss: `Dataset.cluster(lower_bound, upper_bound)` is not k-means, it is
"group all the connected values between two bounds", which is exactly
connected-component labelling. `Dataset.sieve` wraps `gdal.SieveFilter`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np
from pydantic import model_validator

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["ConnectedComponents", "Sieve"]


class ConnectedComponents(RasterOperation):
    """Label each contiguous run of in-range cells with its own number.

    Cells outside `[lower_bound, upper_bound]` become 0, and each connected
    region of in-range cells gets 1, 2, 3 and so on - so counting regions is
    reading the maximum, and selecting one is comparing against its label.

    A no-data cell is outside any finite range, so it becomes background too:
    `[[1, 1, 0], [0, -1, 0], [0, 0, 1]]` with `no_data_value=-1` labels as
    `[[1, 1, 0], [0, 0, 0], [0, 0, 2]]`, where the void and the real background
    are both 0. That is the right default - a void is not a patch - but it
    means the label raster cannot distinguish "not in a patch" from "no data
    here"; read that back from the source raster if it matters.

    Examples:
        - Two separate patches of ones get two labels:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> mask = np.zeros((5, 5), dtype="int16")
            >>> mask[0:2, 0:2] = 1
            >>> mask[4, 4] = 1
            >>> raster = Dataset.from_array(
            ...     arr=mask,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-1,
            ... )
            >>> labels = np.asarray(ConnectedComponents()(raster).read_array())
            >>> int(labels.max()), int(labels[0, 0]), int(labels[4, 4])
            (2, 1, 2)

            ```
    """

    lower_bound: float = 1.0
    upper_bound: float = 1.0
    band: int = 0
    dtype: str = "int32"

    @model_validator(mode="after")
    def _bounds_ordered(self) -> ConnectedComponents:
        """Reject bounds that exclude everything.

        Returns:
            The operation, once its bounds can match a value.

        Raises:
            ValueError: If the lower bound exceeds the upper one, which would
                label nothing and is always a mistake.
        """
        if self.lower_bound > self.upper_bound:
            raise ValueError(
                f"lower_bound {self.lower_bound} is above upper_bound "
                f"{self.upper_bound}, so no cell could be in range"
            )
        return self

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster of region labels.

        Args:
            ds: The source raster, usually a mask or a classification.

        Returns:
            A new single-band raster whose values are 0 for out-of-range cells
            and a distinct positive integer per connected region.
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        if self.band:
            # `cluster` reads the first band, so select first when asked for
            # another one rather than silently labelling the wrong band.
            source = PyramidsDataset.dataset_like(
                ds, np.asarray(ds.read_array(band=self.band))
            )
        else:
            source = ds
        labels, _next_label, _coordinates, _values = source.cluster(
            self.lower_bound, self.upper_bound
        )
        return PyramidsDataset.dataset_like(ds, np.asarray(labels).astype(self.dtype))


class Sieve(RasterOperation):
    """Remove patches smaller than `min_patch_cells`, filling them from neighbours.

    This is what makes a per-pixel classification usable: without it a
    clustering or classifier output is salt-and-pepper, and every area
    statistic computed from it counts noise as land cover.

    Examples:
        - A single stray cell is dropped while a 2x2 patch survives:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> mask = np.zeros((5, 5), dtype="int16")
            >>> mask[0:2, 0:2] = 1
            >>> mask[4, 4] = 1
            >>> raster = Dataset.from_array(
            ...     arr=mask,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=-1,
            ... )
            >>> kept = np.asarray(Sieve(min_patch_cells=2)(raster).read_array())
            >>> int(kept[0, 0]), int(kept[4, 4])
            (1, 0)

            ```
    """

    min_patch_cells: int = 4
    connectedness: Literal[4, 8] = 4
    band: int = 0

    @model_validator(mode="after")
    def _threshold_is_meaningful(self) -> Sieve:
        """Reject a threshold that would remove nothing.

        Returns:
            The operation, once its threshold can drop a patch.

        Raises:
            ValueError: If the threshold is below 2, since a one-cell minimum
                keeps every patch and the operation would be a no-op.
        """
        if self.min_patch_cells < 2:
            raise ValueError(
                f"min_patch_cells must be at least 2, got {self.min_patch_cells}; "
                "a threshold of 1 keeps every patch"
            )
        return self

    def apply(self, ds: Dataset) -> Dataset:
        """Return the raster with small patches removed.

        Args:
            ds: The source raster, usually a classification.

        Returns:
            A new raster in which every surviving patch is at least
            `min_patch_cells` cells.
        """
        return ds.sieve(
            self.min_patch_cells, band=self.band, connectedness=self.connectedness
        )
