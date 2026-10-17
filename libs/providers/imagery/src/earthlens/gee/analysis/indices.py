"""Normalized-difference spectral indices over two bands of a fetched raster.

The family is one formula, `(a - b) / (a + b)`, with named presets for the
combinations that have conventional names. `Ndvi`, `Ndwi` and `Ndbi` differ from
`NormalizedDifference` only in which bands they default to and what they are
called, so the arithmetic - and its division-by-zero handling - lives in one
place.

Bands are addressed by zero-based index rather than by name, because the band
order of a fetched raster follows the request's band list, which the caller
wrote. A name-based lookup would need the catalog, and the catalog describes the
asset rather than the raster that came back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import numpy as np

from earthlens.gee.analysis._arrays import read_masked, result_like
from earthlens.gee.analysis.base import RasterOperation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyramids.dataset import Dataset

__all__ = [
    "Ndbi",
    "Ndvi",
    "Ndwi",
    "NormalizedDifference",
]


class NormalizedDifference(RasterOperation):
    """`(a - b) / (a + b)` over two bands, written to a single-band raster.

    Where both bands are zero the denominator vanishes. Those cells become the
    raster's no-data value rather than an infinity or a `RuntimeWarning`,
    because a normalized difference is undefined there - a zero reflectance in
    both bands means the sensor saw nothing, not that the index is zero.

    Examples:
        - The index of a two-band raster, read back as an array:
            ```python
            >>> import numpy as np
            >>> from pyramids.base.georeference import GeoReference
            >>> from pyramids.dataset import Dataset
            >>> stack = np.stack([
            ...     np.full((2, 2), 0.6, dtype="float32"),
            ...     np.full((2, 2), 0.2, dtype="float32"),
            ... ])
            >>> raster = Dataset.from_array(
            ...     arr=stack,
            ...     geo_ref=GeoReference(top_left_corner=(31.0, 30.0), cell_size=0.01, epsg=4326),
            ...     no_data_value=np.nan,
            ... )
            >>> index = NormalizedDifference(a=0, b=1)(raster)
            >>> float(np.asarray(index.read_array())[0, 0])
            0.5

            ```
    """

    a: int = 0
    b: int = 1

    def apply(self, ds: Dataset) -> Dataset:
        """Return a one-band raster holding the index.

        Args:
            ds: The source raster; must carry both configured bands.

        Returns:
            A new single-band `pyramids.dataset.Dataset` on `ds`' grid.

        Raises:
            IndexError: If either band index is outside the raster.
        """
        for index in (self.a, self.b):
            if not 0 <= index < ds.band_count:
                raise IndexError(
                    f"{self.name} asked for band {index} of a raster with "
                    f"{ds.band_count} band(s)"
                )
        # Masked reads, so a void does not become `(-9999 - -9999) / (-9999 +
        # -9999)` = -0.0, which is a perfectly plausible index value.
        first = read_masked(ds, self.a)
        second = read_masked(ds, self.b)
        total = first + second
        # `np.where` alone would still evaluate the division everywhere and
        # warn; masking the denominator first keeps the computation quiet and
        # leaves the undefined cells as NaN for the sentinel below.
        with np.errstate(invalid="ignore", divide="ignore"):
            index = np.where(total == 0, np.nan, (first - second) / total)
        return result_like(ds, index, dtype="float32", no_data=float("nan"))


class _NormalizedDifferencePreset(RasterOperation):
    """Shared `apply` for a named two-band normalized difference.

    A preset does **not** subclass `NormalizedDifference`: that would inherit
    `a` and `b`, so `Ndvi(a=0, b=1)` would construct successfully and then be
    ignored by `apply` - exactly the silently-accepted keyword `extra="forbid"`
    exists to prevent. What the presets do share is the delegation, which was
    written out three times, so it lives here once.

    A subclass names its two fields in `_band_fields`, in numerator order, and
    adds nothing else. A fourth preset is then two lines.
    """

    #: The subclass's own field names, in `(a, b)` order.
    _band_fields: ClassVar[tuple[str, str]] = ()

    def apply(self, ds: Dataset) -> Dataset:
        """Return the index raster for this preset's two bands.

        Args:
            ds: The source raster.

        Returns:
            A single-band raster of index values in `[-1, 1]`.
        """
        first, second = self._band_fields
        return NormalizedDifference(
            a=getattr(self, first), b=getattr(self, second)
        ).apply(ds)


class Ndvi(_NormalizedDifferencePreset):
    """Normalized difference vegetation index, `(nir - red) / (nir + red)`."""

    _band_fields: ClassVar[tuple[str, str]] = ("nir", "red")

    nir: int = 0
    red: int = 1


class Ndwi(_NormalizedDifferencePreset):
    """Normalized difference water index, `(green - nir) / (green + nir)`."""

    _band_fields: ClassVar[tuple[str, str]] = ("green", "nir")

    green: int = 0
    nir: int = 1


class Ndbi(_NormalizedDifferencePreset):
    """Normalized difference built-up index, `(swir - nir) / (swir + nir)`."""

    _band_fields: ClassVar[tuple[str, str]] = ("swir", "nir")

    swir: int = 0
    nir: int = 1
