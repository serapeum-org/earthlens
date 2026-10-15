"""Grey-level co-occurrence texture measures over a moving window.

Earth Engine's `glcmTexture` computes these server-side. Client-side there is no
pyramids primitive for it, but there is one for the machinery it needs:
`Dataset.focal_apply` hands a callable the flat window, which this module
reshapes back to `(2*radius+1, 2*radius+1)` and turns into a co-occurrence
matrix. So the texture measures are built on the existing focal path rather than
on a new traversal.

Values are quantised to `levels` grey levels before counting pairs, because a
co-occurrence matrix over continuous reflectance would be almost all zeros and
measure nothing. 16 levels is the usual compromise between resolution and a
matrix dense enough to be meaningful in a small window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np
from pydantic import field_validator

from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = ["MEASURES", "Texture"]

#: The measures this operation can compute from a co-occurrence matrix.
MEASURES = ("contrast", "dissimilarity", "homogeneity", "energy", "entropy")


def _cooccurrence(window: np.ndarray, levels: int) -> np.ndarray:
    """Return the normalised horizontal co-occurrence matrix of a window.

    Pairs are counted both ways, which makes the matrix symmetric and the
    measures rotation-invariant for the horizontal offset.

    Args:
        window: The square window, already quantised to `levels` grey levels.
        levels: The number of grey levels.

    Returns:
        A `(levels, levels)` matrix summing to one, or zeros when the window
        holds no valid pair.
    """
    left = window[:, :-1].ravel()
    right = window[:, 1:].ravel()
    valid = ~(np.isnan(left) | np.isnan(right))
    matrix = np.zeros((levels, levels), dtype="float64")
    if not valid.any():
        return matrix
    first = left[valid].astype("int64")
    second = right[valid].astype("int64")
    np.add.at(matrix, (first, second), 1.0)
    np.add.at(matrix, (second, first), 1.0)
    total = matrix.sum()
    return matrix / total if total else matrix


def _measure(matrix: np.ndarray, measure: str) -> float:
    """Reduce a co-occurrence matrix to one number.

    Args:
        matrix: A normalised co-occurrence matrix.
        measure: One of `MEASURES`.

    Returns:
        The measure's value, or `nan` for an empty matrix.
    """
    if not matrix.any():
        return float("nan")
    levels = matrix.shape[0]
    rows, columns = np.indices((levels, levels))
    difference = np.abs(rows - columns)
    if measure == "contrast":
        return float((matrix * difference**2).sum())
    if measure == "dissimilarity":
        return float((matrix * difference).sum())
    if measure == "homogeneity":
        return float((matrix / (1.0 + difference**2)).sum())
    if measure == "energy":
        return float(np.sqrt((matrix**2).sum()))
    # entropy, the only remaining member of MEASURES
    non_zero = matrix[matrix > 0]
    return float(-(non_zero * np.log2(non_zero)).sum())


class Texture(RasterOperation):
    """A grey-level co-occurrence texture measure per window.

    Examples:
        - A constant field has no texture: contrast is zero, homogeneity one:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> flat = Dataset.from_array(
            ...     arr=np.full((7, 7), 5.0, dtype="float32"),
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ... )
            >>> contrast = np.asarray(Texture(measure="contrast")(flat).read_array())
            >>> homogeneity = np.asarray(Texture(measure="homogeneity")(flat).read_array())
            >>> float(contrast[3, 3]), float(homogeneity[3, 3])
            (0.0, 1.0)

            ```
    """

    measure: Literal[
        "contrast", "dissimilarity", "homogeneity", "energy", "entropy"
    ] = "contrast"
    radius: int = 1
    levels: int = 16
    band: int = 0

    @field_validator("radius")
    @classmethod
    def _window_wide_enough(cls, value: int) -> int:
        """Reject a radius with no horizontal pair to count.

        Args:
            value: The configured radius.

        Returns:
            The radius unchanged when usable.

        Raises:
            ValueError: If the radius is below 1, which leaves a single cell
                and therefore no pair of neighbours to co-occur.
        """
        if value < 1:
            raise ValueError(
                f"radius must be at least 1, got {value}; a single cell has no "
                "neighbouring pair to count"
            )
        return value

    @field_validator("levels")
    @classmethod
    def _enough_levels(cls, value: int) -> int:
        """Reject a quantisation too coarse to distinguish anything.

        Args:
            value: The configured number of grey levels.

        Returns:
            The level count unchanged when usable.

        Raises:
            ValueError: If fewer than two levels, which collapses every window
                to one grey value and makes every measure constant.
        """
        if value < 2:
            raise ValueError(
                f"levels must be at least 2, got {value}; one grey level makes "
                "every measure the same number"
            )
        return value

    @property
    def name(self) -> str:
        """The operation and the measure, so a chain names what it computed."""
        return f"Texture({self.measure})"

    def apply(self, ds: Dataset) -> Dataset:
        """Return a raster of the configured texture measure.

        The band is quantised to `levels` grey levels across its own min-max
        range before the windows are counted, so the measure describes relative
        structure rather than absolute values.

        Args:
            ds: The source raster.

        Returns:
            A new single-band raster of texture values.
        """
        from pyramids.dataset import Dataset as PyramidsDataset

        values = np.asarray(ds.read_array(band=self.band), dtype="float64")
        finite = values[np.isfinite(values)]
        if finite.size:
            low, high = float(finite.min()), float(finite.max())
            span = high - low
            quantised = (
                np.zeros_like(values)
                if span == 0
                else np.floor((values - low) / span * (self.levels - 1))
            )
        else:
            quantised = values
        quantised = np.where(np.isfinite(values), quantised, np.nan)
        side = 2 * self.radius + 1
        levels = self.levels
        measure = self.measure

        def _window_texture(flat) -> float:  # noqa: ANN001 - pyramids passes the window
            """Reduce one window to its texture measure."""
            window = np.asarray(flat, dtype="float64")
            if window.size != side * side:
                # An edge window is padded by the filter; reshape only when it
                # is square, otherwise there is no spatial structure to read.
                return float("nan")
            return _measure(_cooccurrence(window.reshape(side, side), levels), measure)

        quantised_ds = PyramidsDataset.dataset_like(ds, quantised.astype("float32"))
        return PyramidsDataset.dataset_like(
            ds, quantised_ds.focal_apply(_window_texture, radius=self.radius, band=0)
        )
