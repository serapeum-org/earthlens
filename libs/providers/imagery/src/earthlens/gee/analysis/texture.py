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

from functools import lru_cache, partial
from typing import TYPE_CHECKING, Literal

import numpy as np
from pydantic import field_validator

from earthlens.gee.analysis._arrays import read_masked, result_like
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


@lru_cache(maxsize=16)
def _difference_matrix(levels: int) -> np.ndarray:
    """Return `|i - j|` for a `levels` x `levels` matrix, built once per size.

    Every measure weights a co-occurrence by how far off the diagonal it sits.
    Rebuilding that weighting inside the window loop cost two array
    allocations per window - 3,200 of them on a 40x40 raster - for a value
    that depends only on the level count.

    Args:
        levels: The number of grey levels.

    Returns:
        A read-only `(levels, levels)` array of absolute index differences.
    """
    rows, columns = np.indices((levels, levels))
    difference = np.abs(rows - columns).astype("float64")
    difference.setflags(write=False)
    return difference


def _texture_of_window(flat, *, side: int, levels: int, measure: str) -> float:  # noqa: ANN001
    """Reduce one flat window to its texture measure.

    At module scope rather than nested inside `apply`, so it is importable and
    testable on its own and the captured state is an explicit `partial`.

    `scipy.ndimage.generic_filter`, which pyramids' `focal_apply` wraps, pads
    the border rather than shrinking the window, so every window holds exactly
    `side * side` values - measured: only size 9 is ever seen for radius 1, and
    only 25 for radius 2. An earlier version guarded against a short window and
    returned `nan`; that branch was unreachable and its comment claimed the
    opposite of what the filter does.

    Args:
        flat: The window, flattened, with no-data cells as `NaN`.
        side: The window's edge length, `2 * radius + 1`.
        levels: The number of grey levels the band was quantised to.
        measure: One of `MEASURES`.

    Returns:
        The measure's value for that window.
    """
    window = np.asarray(flat, dtype="float64").reshape(side, side)
    return _measure(_cooccurrence(window, levels), measure)


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
    difference = _difference_matrix(matrix.shape[0])
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
        # Masked: quantising over the raw array lets one -9999 set the low
        # end of the range, which pushes every real value into the top bin and
        # flattens the texture of the whole raster.
        values = read_masked(ds, self.band)
        finite = values[np.isfinite(values)]
        if finite.size:
            low, high = float(finite.min()), float(finite.max())
            span = high - low
            with np.errstate(invalid="ignore"):
                # The array still holds NaN at the voids, so `floor` warns
                # "invalid value encountered in floor" - harmless (the NaN is
                # masked back in on the next line) but a crash under `-W error`.
                quantised = (
                    np.zeros_like(values)
                    if span == 0
                    else np.floor((values - low) / span * (self.levels - 1))
                )
        else:
            quantised = values
        quantised = np.where(np.isfinite(values), quantised, np.nan)
        reducer = partial(
            _texture_of_window,
            side=2 * self.radius + 1,
            levels=self.levels,
            measure=self.measure,
        )
        # The intermediate carries NaN at the voids, declared as such, so
        # `focal_apply` hands the window NaN rather than a sentinel value.
        quantised_ds = result_like(ds, quantised, dtype="float32", no_data=float("nan"))
        return result_like(
            ds,
            quantised_ds.focal_apply(reducer, radius=self.radius, band=0),
            dtype="float32",
            no_data=float("nan"),
        )
